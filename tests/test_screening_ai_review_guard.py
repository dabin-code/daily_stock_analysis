from __future__ import annotations

from copy import deepcopy

import pytest

from src.schemas.screening_ai_review import (
    ScreeningAiReviewResult,
    build_rules_fallback_review,
)
from src.schemas.trading_types import CandidateDecision, EntryMaturity, SetupType, TradeStage
from src.services.screening_ai_review_guard import ScreeningAiReviewGuard


def _make_candidate(**overrides):
    base = CandidateDecision(code="600519", name="贵州茅台", trade_stage=TradeStage.FOCUS)
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _make_review(**overrides):
    values = {
        "environment_ok": True,
        "trade_stage": TradeStage.ADD_ON_STRENGTH,
        "entry_maturity": EntryMaturity.HIGH,
        "setup_type": SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
        "risk_level": "low",
        "initial_position": "1/2",
        "stop_loss_rule": "跌破当前层级止损位",
        "take_profit_plan": "沿趋势止盈",
        "invalidation_rule": "跌破结构失效",
        "reasoning_summary": "可执行",
        "confidence": 0.9,
        "result_source": "rules_plus_ai",
        "is_fallback": False,
        "fallback_reason": None,
    }
    values.update(overrides)
    return ScreeningAiReviewResult(**values)


def test_screening_ai_review_result_exposes_contract_fields() -> None:
    review = ScreeningAiReviewResult(
        environment_ok=True,
        trade_stage=TradeStage.FOCUS,
        entry_maturity=EntryMaturity.MEDIUM,
        setup_type=SetupType.TREND_BREAKOUT,
        risk_level="medium",
        initial_position="1/4",
        stop_loss_rule="跌破MA20离场",
        take_profit_plan="沿5日线止盈",
        invalidation_rule="放量长阴失效",
        reasoning_summary="结构仍完整",
        confidence=0.72,
        result_source="rules_plus_ai",
        is_fallback=False,
        fallback_reason=None,
    )

    payload = review.to_payload()

    assert payload["environment_ok"] is True
    assert payload["trade_stage"] == "focus"
    assert payload["entry_maturity"] == "medium"
    assert payload["setup_type"] == "trend_breakout"
    assert payload["risk_level"] == "medium"
    assert payload["initial_position"] == "1/4"
    assert payload["stop_loss_rule"] == "跌破MA20离场"
    assert payload["take_profit_plan"] == "沿5日线止盈"
    assert payload["invalidation_rule"] == "放量长阴失效"
    assert payload["reasoning_summary"] == "结构仍完整"
    assert payload["confidence"] == 0.72
    assert payload["fallback_reason"] is None
    assert payload["result_source"] == "rules_plus_ai"


def test_guard_caps_trade_stage_when_environment_is_not_ok() -> None:
    candidate = _make_candidate(trade_stage=TradeStage.ADD_ON_STRENGTH)
    review = ScreeningAiReviewResult(
        environment_ok=False,
        trade_stage=TradeStage.ADD_ON_STRENGTH,
        entry_maturity=EntryMaturity.HIGH,
        setup_type=SetupType.TREND_BREAKOUT,
        risk_level="high",
        initial_position="1/4",
        stop_loss_rule="跌破MA20离场",
        take_profit_plan="沿5日线止盈",
        invalidation_rule="放量长阴失效",
        reasoning_summary="环境一般",
        confidence=0.82,
        result_source="rules_plus_ai",
        is_fallback=False,
        fallback_reason=None,
    )

    guarded = ScreeningAiReviewGuard().apply(candidate, review)

    assert guarded.trade_stage == TradeStage.WATCH
    assert "environment_constraint" in guarded.downgrade_reasons


def test_guard_blocks_high_entry_maturity_when_setup_is_none() -> None:
    candidate = _make_candidate(setup_type=SetupType.NONE)
    review = ScreeningAiReviewResult(
        environment_ok=True,
        trade_stage=TradeStage.FOCUS,
        entry_maturity=EntryMaturity.HIGH,
        setup_type=SetupType.NONE,
        risk_level="medium",
        initial_position=None,
        stop_loss_rule=None,
        take_profit_plan=None,
        invalidation_rule=None,
        reasoning_summary="暂无明确形态",
        confidence=0.55,
        result_source="rules_plus_ai",
        is_fallback=False,
        fallback_reason=None,
    )

    guarded = ScreeningAiReviewGuard().apply(candidate, review)

    assert guarded.entry_maturity != EntryMaturity.HIGH
    assert "setup_constraint" in guarded.downgrade_reasons


def test_guard_downgrades_execution_stage_when_plan_fields_are_missing() -> None:
    candidate = _make_candidate(trade_stage=TradeStage.PROBE_ENTRY)
    review = ScreeningAiReviewResult(
        environment_ok=True,
        trade_stage=TradeStage.PROBE_ENTRY,
        entry_maturity=EntryMaturity.HIGH,
        setup_type=SetupType.TREND_BREAKOUT,
        risk_level="medium",
        initial_position="1/4",
        stop_loss_rule="",
        take_profit_plan="沿5日线止盈",
        invalidation_rule="放量长阴失效",
        reasoning_summary="计划不完整",
        confidence=0.7,
        result_source="rules_plus_ai",
        is_fallback=False,
        fallback_reason=None,
    )

    guarded = ScreeningAiReviewGuard().apply(candidate, review)

    assert guarded.trade_stage == TradeStage.FOCUS
    assert "missing_stop_anchor" in guarded.downgrade_reasons


def test_guard_caps_ai_stage_to_rule_constraints() -> None:
    candidate = _make_candidate(trade_stage=TradeStage.WATCH)
    review = ScreeningAiReviewResult(
        environment_ok=True,
        trade_stage=TradeStage.ADD_ON_STRENGTH,
        entry_maturity=EntryMaturity.HIGH,
        setup_type=SetupType.TREND_BREAKOUT,
        risk_level="low",
        initial_position="1/2",
        stop_loss_rule="跌破MA20离场",
        take_profit_plan="沿5日线止盈",
        invalidation_rule="放量长阴失效",
        reasoning_summary="AI 比规则更激进",
        confidence=0.9,
        result_source="rules_plus_ai",
        is_fallback=False,
        fallback_reason=None,
    )

    guarded = ScreeningAiReviewGuard().apply(candidate, review)

    assert guarded.trade_stage == TradeStage.WATCH
    assert "rule_conflict" in guarded.downgrade_reasons


def test_guard_uses_rule_candidate_environment_even_when_ai_claims_it_is_ok() -> None:
    candidate = _make_candidate(
        environment_ok=False,
        trade_stage=TradeStage.ADD_ON_STRENGTH,
    )

    guarded = ScreeningAiReviewGuard().apply(candidate, _make_review())

    assert guarded.trade_stage == TradeStage.WATCH
    assert "environment_constraint" in guarded.downgrade_reasons


@pytest.mark.parametrize(
    ("stage", "status"),
    [
        ("stale", "confirmation_too_old"),
        ("extended", "extended"),
        ("invalidated", "candidate_invalidated"),
        ("major_unverified", "adjustment_unknown"),
        ("breakout_failed", "below_r2"),
    ],
)
def test_guard_blocks_non_actionable_v2_stages(stage: str, status: str) -> None:
    candidate = _make_candidate(
        trade_stage=TradeStage.ADD_ON_STRENGTH,
        setup_type=SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
        factor_snapshot={
            "bottom_divergence_v2_stage": stage,
            "bottom_divergence_v2_actionability_status": status,
            "bottom_divergence_v2_major_actionable_entry": False,
            "bottom_divergence_v2_stop_loss_price": 8.8,
            "bottom_divergence_v2_layered_buy_points": [
                {"level": "r2", "price": 16.0, "stop": 8.8, "triggered": True}
            ],
        },
    )

    guarded = ScreeningAiReviewGuard().apply(candidate, _make_review())

    assert guarded.trade_stage == TradeStage.FOCUS
    assert "v2_non_actionable_stage" in guarded.downgrade_reasons


def test_guard_blocks_major_when_current_actionability_is_false() -> None:
    candidate = _make_candidate(
        trade_stage=TradeStage.ADD_ON_STRENGTH,
        setup_type=SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
        factor_snapshot={
            "bottom_divergence_v2_stage": "major_actionable",
            "bottom_divergence_v2_actionability_status": "extended",
            "bottom_divergence_v2_major_breakout": True,
            "bottom_divergence_v2_major_actionable_entry": False,
            "bottom_divergence_v2_stop_loss_price": 8.8,
            "bottom_divergence_v2_layered_buy_points": [
                {"level": "r2", "price": 16.0, "stop": 8.8, "triggered": True}
            ],
        },
    )

    guarded = ScreeningAiReviewGuard().apply(candidate, _make_review())

    assert guarded.trade_stage == TradeStage.FOCUS
    assert "v2_major_not_actionable" in guarded.downgrade_reasons


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (
            {
                "bottom_divergence_v2_stage": "early",
                "bottom_divergence_v2_actionability_status": (
                    "major_not_confirmed"
                ),
                "bottom_divergence_v2_stop_loss_price": 8.8,
                "bottom_divergence_v2_layered_buy_points": [],
            },
            "v2_missing_current_buy_point",
        ),
        (
            {
                "bottom_divergence_v2_stage": "near_cleared",
                "bottom_divergence_v2_actionability_status": (
                    "major_not_confirmed"
                ),
                "bottom_divergence_v2_stop_loss_price": None,
                "bottom_divergence_v2_layered_buy_points": [
                    {
                        "level": "r1",
                        "price": 14.0,
                        "stop": None,
                        "triggered": True,
                    }
                ],
            },
            "v2_missing_current_stop",
        ),
    ],
)
def test_guard_blocks_v2_without_current_stage_execution_anchors(
    snapshot: dict,
    reason: str,
) -> None:
    candidate = _make_candidate(
        trade_stage=TradeStage.ADD_ON_STRENGTH,
        setup_type=SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
        factor_snapshot=snapshot,
    )

    guarded = ScreeningAiReviewGuard().apply(candidate, _make_review())

    assert guarded.trade_stage == TradeStage.FOCUS
    assert reason in guarded.downgrade_reasons


@pytest.mark.parametrize(
    (
        "stage",
        "status",
        "rule_stage",
        "ai_stage",
        "level",
        "major_actionable",
    ),
    [
        (
            "early",
            "major_not_confirmed",
            TradeStage.PROBE_ENTRY,
            TradeStage.PROBE_ENTRY,
            "early",
            False,
        ),
        (
            "near_cleared",
            "major_not_confirmed",
            TradeStage.ADD_ON_STRENGTH,
            TradeStage.ADD_ON_STRENGTH,
            "r1",
            False,
        ),
        (
            "major_actionable",
            "actionable",
            TradeStage.ADD_ON_STRENGTH,
            TradeStage.ADD_ON_STRENGTH,
            "r2",
            True,
        ),
    ],
)
def test_guard_preserves_actionable_v2_stage_within_rule_cap(
    stage: str,
    status: str,
    rule_stage: TradeStage,
    ai_stage: TradeStage,
    level: str,
    major_actionable: bool,
) -> None:
    candidate = _make_candidate(
        trade_stage=rule_stage,
        setup_type=SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
        factor_snapshot={
            "bottom_divergence_v2_stage": stage,
            "bottom_divergence_v2_actionability_status": status,
            "bottom_divergence_v2_major_actionable_entry": major_actionable,
            "bottom_divergence_v2_stop_loss_price": 8.8,
            "bottom_divergence_v2_layered_buy_points": [
                {"level": level, "price": 12.0, "stop": 8.8, "triggered": True}
            ],
        },
    )

    guarded = ScreeningAiReviewGuard().apply(
        candidate,
        _make_review(trade_stage=ai_stage),
    )

    assert guarded.trade_stage == ai_stage
    assert not guarded.downgrade_reasons


@pytest.mark.parametrize(
    "status",
    ["", None, "future_status"],
)
def test_guard_fail_closes_unknown_v2_actionability_status(
    status: str | None,
) -> None:
    candidate = _make_candidate(
        trade_stage=TradeStage.ADD_ON_STRENGTH,
        setup_type=SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
        factor_snapshot={
            "bottom_divergence_v2_stage": "early",
            "bottom_divergence_v2_actionability_status": status,
            "bottom_divergence_v2_stop_loss_price": 8.8,
            "bottom_divergence_v2_layered_buy_points": [
                {
                    "level": "early",
                    "price": 12.0,
                    "stop": 8.8,
                    "triggered": True,
                }
            ],
        },
    )

    guarded = ScreeningAiReviewGuard().apply(candidate, _make_review())

    assert guarded.trade_stage == TradeStage.FOCUS
    assert "v2_actionability_status_not_allowed" in guarded.downgrade_reasons


@pytest.mark.parametrize(
    ("stage", "status", "rule_stage", "ai_stage", "expected_stage"),
    [
        (
            "early",
            "major_not_confirmed",
            TradeStage.PROBE_ENTRY,
            TradeStage.PROBE_ENTRY,
            TradeStage.PROBE_ENTRY,
        ),
        (
            "near_cleared",
            "major_not_confirmed",
            TradeStage.PROBE_ENTRY,
            TradeStage.ADD_ON_STRENGTH,
            TradeStage.PROBE_ENTRY,
        ),
    ],
)
def test_guard_allows_real_early_near_status_without_exceeding_rule_stage(
    stage: str,
    status: str,
    rule_stage: TradeStage,
    ai_stage: TradeStage,
    expected_stage: TradeStage,
) -> None:
    level = "early" if stage == "early" else "r1"
    candidate = _make_candidate(
        trade_stage=rule_stage,
        setup_type=SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
        factor_snapshot={
            "bottom_divergence_v2_stage": stage,
            "bottom_divergence_v2_actionability_status": status,
            "bottom_divergence_v2_major_actionable_entry": False,
            "bottom_divergence_v2_stop_loss_price": 8.8,
            "bottom_divergence_v2_layered_buy_points": [
                {
                    "level": level,
                    "price": 12.0,
                    "stop": 8.8,
                    "triggered": True,
                }
            ],
        },
    )

    guarded = ScreeningAiReviewGuard().apply(
        candidate,
        _make_review(trade_stage=ai_stage),
    )

    assert guarded.trade_stage == expected_stage
    if ai_stage != expected_stage:
        assert "rule_conflict" in guarded.downgrade_reasons


@pytest.mark.parametrize(
    ("stage", "level"),
    [
        ("early", "early"),
        ("near_cleared", "r1"),
    ],
)
def test_guard_adjustment_unknown_preserves_observation_but_blocks_execution(
    stage: str,
    level: str,
) -> None:
    candidate = _make_candidate(
        trade_stage=TradeStage.ADD_ON_STRENGTH,
        setup_type=SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
        factor_snapshot={
            "bottom_divergence_v2_stage": stage,
            "bottom_divergence_v2_actionability_status": (
                "adjustment_unknown"
            ),
            "bottom_divergence_v2_major_actionable_entry": False,
            "bottom_divergence_v2_stop_loss_price": 8.8,
            "bottom_divergence_v2_layered_buy_points": [
                {
                    "level": level,
                    "price": 12.0,
                    "stop": 8.8,
                    "triggered": True,
                }
            ],
        },
    )

    guarded = ScreeningAiReviewGuard().apply(candidate, _make_review())

    assert guarded.trade_stage == TradeStage.FOCUS
    assert guarded.ai_operation_advice == TradeStage.FOCUS.value
    assert guarded.initial_position is None
    assert "v2_adjustment_unknown" in guarded.downgrade_reasons


@pytest.mark.parametrize(
    ("major_actionable", "status", "should_preserve"),
    [
        (True, "actionable", True),
        (False, "actionable", False),
        (True, "major_not_confirmed", False),
        (True, "future_status", False),
    ],
)
def test_guard_requires_exact_major_actionable_combination(
    major_actionable: bool,
    status: str,
    should_preserve: bool,
) -> None:
    candidate = _make_candidate(
        trade_stage=TradeStage.ADD_ON_STRENGTH,
        setup_type=SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
        factor_snapshot={
            "bottom_divergence_v2_stage": "major_actionable",
            "bottom_divergence_v2_actionability_status": status,
            "bottom_divergence_v2_major_actionable_entry": major_actionable,
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

    guarded = ScreeningAiReviewGuard().apply(candidate, _make_review())

    expected = (
        TradeStage.ADD_ON_STRENGTH
        if should_preserve
        else TradeStage.FOCUS
    )
    assert guarded.trade_stage == expected


def test_guard_does_not_mutate_review_and_repeated_calls_do_not_accumulate() -> None:
    candidate = _make_candidate(
        trade_stage=TradeStage.ADD_ON_STRENGTH,
        setup_type=SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
        factor_snapshot={
            "bottom_divergence_v2_stage": "stale",
            "bottom_divergence_v2_actionability_status": (
                "confirmation_too_old"
            ),
        },
    )
    review = _make_review(
        downgrade_reasons=["existing_reason"],
        raw_payload={
            "trade_stage": "add_on_strength",
            "initial_position": "1/2",
            "nested": {"values": ["keep"]},
        },
    )
    original_payload = deepcopy(review.to_payload())
    original_dict = deepcopy(review.__dict__)

    first = ScreeningAiReviewGuard().apply(candidate, review)
    second = ScreeningAiReviewGuard().apply(candidate, review)

    assert review.to_payload() == original_payload
    assert review.__dict__ == original_dict
    assert first.downgrade_reasons == second.downgrade_reasons
    assert first.downgrade_reasons == [
        "existing_reason",
        "v2_non_actionable_stage",
    ]
    assert first.raw_payload is not review.raw_payload
    assert first.raw_payload["nested"] is not review.raw_payload["nested"]


def test_build_rules_fallback_review_marks_fallback_source() -> None:
    candidate = _make_candidate(
        trade_stage=TradeStage.FOCUS,
        entry_maturity=EntryMaturity.MEDIUM,
        setup_type=SetupType.TREND_BREAKOUT,
    )

    review = build_rules_fallback_review(candidate, fallback_reason="invalid_json")

    assert review.result_source == "rules_fallback"
    assert review.is_fallback is True
    assert review.fallback_reason == "invalid_json"
    assert review.trade_stage == TradeStage.FOCUS
