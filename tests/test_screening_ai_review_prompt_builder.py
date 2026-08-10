from __future__ import annotations

import json

from src.schemas.trading_types import CandidateDecision, EntryMaturity, SetupType, TradePlan, TradeStage
from src.services.screening_ai_review_prompt_builder import (
    SCREENING_AI_REVIEW_PROMPT_VERSION,
    ScreeningAiReviewPromptBuilder,
)


def _input_payload(prompt: str) -> dict:
    input_text = prompt.split("Input:\n", 1)[1].split("\nOutput schema:", 1)[0]
    return json.loads(input_text)


def test_prompt_builder_uses_structured_sections_and_json_only_contract() -> None:
    candidate = CandidateDecision(
        code="600519",
        name="贵州茅台",
        trade_stage=TradeStage.FOCUS,
        setup_type=SetupType.TREND_BREAKOUT,
        entry_maturity=EntryMaturity.HIGH,
        trade_plan=TradePlan(
            initial_position="1/4",
            stop_loss_rule="跌破MA20离场",
            take_profit_plan="沿5日线止盈",
            invalidation_rule="放量长阴失效",
        ),
        factor_snapshot={"close": 1500.0, "ma20": 1480.0},
    )

    prompt = ScreeningAiReviewPromptBuilder().build(candidate)

    assert SCREENING_AI_REVIEW_PROMPT_VERSION == "screening_ai_review_v2"
    assert "prompt_version: screening_ai_review_v2" in prompt
    assert '"context"' in prompt
    assert '"market"' in prompt
    assert '"theme"' in prompt
    assert '"stock"' in prompt
    assert '"setup"' in prompt
    assert '"trade_plan"' in prompt
    assert "Return JSON only" in prompt
    assert "cannot override environment/theme hard constraints" in prompt
    assert "missing evidence must downgrade conservatively" in prompt
    assert "dashboard" not in prompt.lower()
    assert "general stock commentary" not in prompt.lower()


def test_prompt_builder_includes_required_output_schema_fields() -> None:
    candidate = CandidateDecision(code="000001", name="平安银行")

    prompt = ScreeningAiReviewPromptBuilder().build(candidate)

    for field_name in (
        "environment_ok",
        "trade_stage",
        "entry_maturity",
        "setup_type",
        "risk_level",
        "initial_position",
        "stop_loss_rule",
        "take_profit_plan",
        "invalidation_rule",
        "reasoning_summary",
        "confidence",
    ):
        assert f'"{field_name}"' in prompt
    assert "bottom_divergence_layered_entry" in prompt


def test_prompt_builder_adds_structured_bottom_divergence_v2_evidence_only_for_new_setup() -> None:
    candidate = CandidateDecision(
        code="001337",
        name="四川黄金",
        setup_type=SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
        factor_snapshot={
            "close": 28.5,
            "bottom_divergence_v2_stage": "major_actionable",
            "bottom_divergence_v2_pattern_code": "macd_price",
            "bottom_divergence_v2_early_strength": 0.87,
            "bottom_divergence_v2_near_zone_lower": 24.1,
            "bottom_divergence_v2_near_zone_upper": 24.8,
            "bottom_divergence_v2_near_zone_score": 0.71,
            "bottom_divergence_v2_major_zone_lower": 27.2,
            "bottom_divergence_v2_major_zone_upper": 27.8,
            "bottom_divergence_v2_major_zone_score": 0.92,
            "bottom_divergence_v2_candidate_version": "primary",
            "bottom_divergence_v2_zone_version": "causal_v2",
            "bottom_divergence_v2_degradation_reasons": ["metadata_partial"],
            "bottom_divergence_v2_major_breakout": True,
            "bottom_divergence_v2_major_actionable_entry": False,
            "bottom_divergence_v2_actionability_status": "extended",
            "bottom_divergence_v2_event_days": 3,
            "bottom_divergence_v2_candidate_records": [
                {
                    "candidate_version": "decoy",
                    "zone": {
                        "r1": {"touch_points": [{"date": "1999-01-01"}]},
                        "r2": {"touch_points": [{"date": "1999-01-02"}]},
                    },
                },
                {
                    "candidate_version": "primary",
                    "zone": {
                        "r1": {
                            "touch_points": [
                                {"date": "2026-07-10"},
                                {"date": "2026-07-03"},
                                {"date": "2026-07-10"},
                            ]
                        },
                        "r2": {
                            "touch_points": [
                                {"date": "2026-07-06"},
                                {"date": "2026-07-02"},
                                {"date": "2026-07-04"},
                                {"date": "2026-07-01"},
                                {"date": "2026-07-03"},
                                {"date": "2026-07-05"},
                                {"date": "2026-07-03"},
                            ]
                        },
                    },
                },
            ],
        },
    )

    payload = _input_payload(ScreeningAiReviewPromptBuilder().build(candidate))

    assert payload["setup"]["bottom_divergence_v2_evidence"] == {
        "stage": "major_actionable",
        "pattern_code": "macd_price",
        "early_strength": 0.87,
        "r1": {
            "lower": 24.1,
            "upper": 24.8,
            "score": 0.71,
            "touch_dates": ["2026-07-03", "2026-07-10"],
        },
        "r2": {
            "lower": 27.2,
            "upper": 27.8,
            "score": 0.92,
            "touch_dates": [
                "2026-07-01",
                "2026-07-02",
                "2026-07-03",
                "2026-07-04",
                "2026-07-05",
            ],
        },
        "candidate_version": "primary",
        "zone_version": "causal_v2",
        "degradation_reasons": ["metadata_partial"],
        "major_breakout": True,
        "major_actionable_entry": False,
        "actionability_status": "extended",
        "event_days": 3,
    }
    assert payload["stock"]["factor_snapshot"]["close"] == 28.5
    assert (
        "bottom_divergence_v2_candidate_records"
        not in payload["stock"]["factor_snapshot"]
    )

    generic = CandidateDecision(
        code="600519",
        setup_type=SetupType.TREND_BREAKOUT,
        factor_snapshot={"bottom_divergence_v2_stage": "early"},
    )
    generic_payload = _input_payload(ScreeningAiReviewPromptBuilder().build(generic))
    assert "bottom_divergence_v2_evidence" not in generic_payload["setup"]


def test_prompt_builder_preserves_none_contract_and_allows_early_without_r2() -> None:
    candidate = CandidateDecision(
        code="001337",
        setup_type=SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
        factor_snapshot={
            "bottom_divergence_v2_stage": "early",
            "bottom_divergence_v2_pattern_code": "macd_price",
            "bottom_divergence_v2_early_strength": 0.66,
            "bottom_divergence_v2_near_zone_lower": 20.0,
            "bottom_divergence_v2_near_zone_upper": 21.0,
            "bottom_divergence_v2_near_zone_score": 0.7,
        },
    )

    prompt = ScreeningAiReviewPromptBuilder().build(candidate)
    evidence = _input_payload(prompt)["setup"]["bottom_divergence_v2_evidence"]

    assert evidence["r2"] == {
        "lower": None,
        "upper": None,
        "score": None,
        "touch_dates": [],
    }
    assert evidence["r1"]["touch_dates"] == []
    assert evidence["candidate_version"] is None
    assert evidence["major_actionable_entry"] is None
    assert "Missing R2 for an early-stage candidate is not insufficient evidence" in prompt


def test_prompt_builder_falls_back_to_direct_v2_touch_date_fields() -> None:
    candidate = CandidateDecision(
        code="001337",
        setup_type=SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
        factor_snapshot={
            "bottom_divergence_v2_stage": "near_cleared",
            "bottom_divergence_v2_candidate_version": "primary",
            "bottom_divergence_v2_near_zone_touch_dates": [
                "2026-07-05",
                "2026-07-01",
                "2026-07-05",
            ],
            "bottom_divergence_v2_major_zone_touch_dates": [
                "2026-07-20",
            ],
        },
    )

    evidence = _input_payload(
        ScreeningAiReviewPromptBuilder().build(candidate)
    )["setup"]["bottom_divergence_v2_evidence"]

    assert evidence["r1"]["touch_dates"] == [
        "2026-07-01",
        "2026-07-05",
    ]
    assert evidence["r2"]["touch_dates"] == ["2026-07-20"]


def test_prompt_builder_states_v2_fail_closed_actionability_rules() -> None:
    candidate = CandidateDecision(
        code="001337",
        setup_type=SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
    )

    prompt = ScreeningAiReviewPromptBuilder().build(candidate)

    assert "Historical major_breakout does not mean the candidate is currently actionable" in prompt
    assert "major_actionable_entry=false" in prompt
    for blocked_value in (
        "stale",
        "extended",
        "invalidated",
        "major_unverified",
        "breakout_failed",
        "adjustment_unknown",
    ):
        assert blocked_value in prompt
    assert "current-stage buy point and stop" in prompt
    assert "must not return probe_entry or add_on_strength" in prompt
