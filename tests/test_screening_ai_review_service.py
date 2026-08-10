from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.schemas.trading_types import CandidateDecision, EntryMaturity, SetupType, TradeStage
from src.services.screening_ai_review_service import ScreeningAiReviewService


def _candidate() -> CandidateDecision:
    return CandidateDecision(
        code="600519",
        name="贵州茅台",
        trade_stage=TradeStage.FOCUS,
        setup_type=SetupType.TREND_BREAKOUT,
        entry_maturity=EntryMaturity.HIGH,
    )


def _valid_json(trade_stage: str = "focus") -> str:
    return (
        "{"
        f'"environment_ok": true,'
        f'"trade_stage": "{trade_stage}",'
        '"entry_maturity": "high",'
        '"setup_type": "trend_breakout",'
        '"risk_level": "medium",'
        '"initial_position": "1/4",'
        '"stop_loss_rule": "跌破MA20离场",'
        '"take_profit_plan": "沿5日线止盈",'
        '"invalidation_rule": "放量长阴失效",'
        '"reasoning_summary": "结构完整",'
        '"confidence": 0.76'
        "}"
    )


def _valid_v2_json(trade_stage: str = "add_on_strength") -> str:
    payload = {
        "environment_ok": True,
        "trade_stage": trade_stage,
        "entry_maturity": "high",
        "setup_type": "bottom_divergence_layered_entry",
        "risk_level": "low",
        "initial_position": "1/2",
        "stop_loss_rule": "跌破当前层级止损位",
        "take_profit_plan": "沿趋势止盈",
        "invalidation_rule": "跌破结构失效",
        "reasoning_summary": "尝试升级",
        "confidence": 0.9,
    }
    return json.dumps(payload, ensure_ascii=False)


def _v2_candidate(stage: str, status: str) -> CandidateDecision:
    return CandidateDecision(
        code="001337",
        name="四川黄金",
        trade_stage=TradeStage.ADD_ON_STRENGTH,
        setup_type=SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
        entry_maturity=EntryMaturity.HIGH,
        factor_snapshot={
            "bottom_divergence_v2_stage": stage,
            "bottom_divergence_v2_actionability_status": status,
            "bottom_divergence_v2_major_breakout": stage.startswith("major"),
            "bottom_divergence_v2_major_actionable_entry": (
                stage == "major_actionable"
            ),
            "bottom_divergence_v2_stop_loss_price": 8.8,
            "bottom_divergence_v2_layered_buy_points": [
                {
                    "level": "r2",
                    "price": 16.0,
                    "stop": 8.8,
                    "triggered": True,
                }
            ],
        },
    )


def test_service_accepts_valid_json_on_first_try() -> None:
    llm_client = MagicMock()
    llm_client.generate_text.return_value = _valid_json()

    review = ScreeningAiReviewService(llm_client=llm_client).review_candidate(_candidate())

    assert review.result_source == "rules_plus_ai"
    assert review.fallback_reason is None
    assert review.retry_count == 0
    assert review.trade_stage == TradeStage.FOCUS


def test_service_retries_once_after_invalid_json_then_accepts_valid_json() -> None:
    llm_client = MagicMock()
    llm_client.generate_text.side_effect = ["not-json", _valid_json("probe_entry")]

    review = ScreeningAiReviewService(llm_client=llm_client).review_candidate(_candidate())

    assert llm_client.generate_text.call_count == 2
    assert review.result_source == "rules_plus_ai"
    assert review.retry_count == 1
    assert review.trade_stage == TradeStage.FOCUS


def test_service_falls_back_after_two_invalid_json_responses() -> None:
    llm_client = MagicMock()
    llm_client.generate_text.side_effect = ["nope", "still-nope"]

    review = ScreeningAiReviewService(llm_client=llm_client).review_candidate(_candidate())

    assert llm_client.generate_text.call_count == 2
    assert review.result_source == "rules_fallback"
    assert review.fallback_reason == "invalid_json"


def test_service_falls_back_on_timeout_or_model_exception() -> None:
    llm_client = MagicMock()
    llm_client.generate_text.side_effect = TimeoutError("llm timeout")

    review = ScreeningAiReviewService(llm_client=llm_client).review_candidate(_candidate())

    assert review.result_source == "rules_fallback"
    assert review.fallback_reason == "timeout"


def test_service_falls_back_when_structured_output_cannot_be_normalized() -> None:
    llm_client = MagicMock()
    llm_client.generate_text.return_value = (
        "{"
        '"environment_ok": true,'
        '"trade_stage": "rocket",'
        '"entry_maturity": "high",'
        '"setup_type": "trend_breakout",'
        '"risk_level": "medium",'
        '"initial_position": "1/4",'
        '"stop_loss_rule": "跌破MA20离场",'
        '"take_profit_plan": "沿5日线止盈",'
        '"invalidation_rule": "放量长阴失效",'
        '"reasoning_summary": "bad enum",'
        '"confidence": 0.76'
        "}"
    )

    review = ScreeningAiReviewService(llm_client=llm_client).review_candidate(_candidate())

    assert review.result_source == "rules_fallback"
    assert review.fallback_reason == "normalize_failed"


def test_service_blocks_r2_when_adjustment_metadata_is_unknown() -> None:
    llm_client = MagicMock()
    llm_client.generate_text.return_value = _valid_v2_json()
    candidate = _v2_candidate("major_actionable", "adjustment_unknown")

    review = ScreeningAiReviewService(llm_client=llm_client).review_candidate(
        candidate
    )

    assert review.trade_stage == TradeStage.FOCUS
    assert review.ai_operation_advice == TradeStage.FOCUS.value
    assert "v2_adjustment_unknown" in review.downgrade_reasons


def test_service_clears_model_execution_advice_after_v2_fail_closed() -> None:
    llm_client = MagicMock()
    payload = json.loads(_valid_v2_json())
    payload.update(
        {
            "initial_position": "1/2",
            "add_rule": "突破R2后继续加仓",
            "entry": "现价买入",
            "entry_price": 16.0,
        }
    )
    llm_client.generate_text.return_value = json.dumps(
        payload,
        ensure_ascii=False,
    )
    candidate = _v2_candidate("stale", "confirmation_too_old")

    review = ScreeningAiReviewService(llm_client=llm_client).review_candidate(
        candidate
    )

    assert review.trade_stage == TradeStage.FOCUS
    assert review.ai_operation_advice == TradeStage.FOCUS.value
    assert review.initial_position is None
    assert review.take_profit_plan is None
    assert review.raw_model_output is None
    assert review.raw_payload is not None
    for field_name in (
        "initial_position",
        "add_rule",
        "entry",
        "entry_price",
        "take_profit_plan",
    ):
        assert review.raw_payload[field_name] is None
    assert review.raw_payload["trade_stage"] == TradeStage.FOCUS.value
    assert review.reasoning_summary == "尝试升级"
    assert review.stop_loss_rule == "跌破当前层级止损位"


@pytest.mark.parametrize("stage", ["stale", "invalidated"])
def test_service_blocks_stale_or_invalidated_v2_candidate(stage: str) -> None:
    llm_client = MagicMock()
    llm_client.generate_text.return_value = _valid_v2_json("probe_entry")
    candidate = _v2_candidate(stage, stage)

    review = ScreeningAiReviewService(llm_client=llm_client).review_candidate(
        candidate
    )

    assert review.trade_stage == TradeStage.FOCUS
    assert review.ai_operation_advice == TradeStage.FOCUS.value
    assert "v2_non_actionable_stage" in review.downgrade_reasons
