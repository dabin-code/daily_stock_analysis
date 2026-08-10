from __future__ import annotations

from copy import deepcopy

from src.schemas.screening_ai_review import ScreeningAiReviewResult
from src.schemas.trading_types import CandidateDecision, EntryMaturity, SetupType, TradeStage
from src.services.bottom_divergence_v2_trade_support import (
    resolve_current_stage_buy_point,
    resolve_current_stage_stop_loss,
)


_STAGE_ORDER = {
    TradeStage.STAND_ASIDE: 0,
    TradeStage.REJECT: 0,
    TradeStage.WATCH: 1,
    TradeStage.FOCUS: 2,
    TradeStage.PROBE_ENTRY: 3,
    TradeStage.ADD_ON_STRENGTH: 4,
}

_V2_ALLOWED_ACTIONABILITY_STATUSES = {
    # actionability_v2 returns major_not_confirmed before R2 confirmation.
    # adjustment_unknown may preserve evidence, but never execution.
    "early": frozenset({"major_not_confirmed"}),
    "near_cleared": frozenset({"major_not_confirmed"}),
    # A current R2 entry is valid only for the detector's explicit success.
    "major_actionable": frozenset({"actionable"}),
}
_EXECUTION_ADVICE_FIELDS = {
    "add_on_rule",
    "add_rule",
    "ai_operation_advice",
    "buy_rule",
    "buy_point",
    "buy_price",
    "entry",
    "entry_price",
    "entry_rule",
    "entry_valid_days",
    "initial_position",
    "position",
    "position_size",
    "take_profit_plan",
}


class ScreeningAiReviewGuard:
    def apply(self, candidate: CandidateDecision, review: ScreeningAiReviewResult) -> ScreeningAiReviewResult:
        guarded = deepcopy(review)

        if (
            not candidate.environment_ok or not guarded.environment_ok
        ) and _is_higher_than(guarded.trade_stage, TradeStage.WATCH):
            guarded.environment_ok = False
            guarded.trade_stage = TradeStage.WATCH
            guarded.downgrade_reasons.append("environment_constraint")

        if guarded.setup_type == SetupType.NONE and guarded.entry_maturity == EntryMaturity.HIGH:
            guarded.entry_maturity = EntryMaturity.MEDIUM
            guarded.downgrade_reasons.append("setup_constraint")

        if guarded.trade_stage in {TradeStage.PROBE_ENTRY, TradeStage.ADD_ON_STRENGTH}:
            if not guarded.stop_loss_rule:
                guarded.trade_stage = TradeStage.FOCUS
                guarded.downgrade_reasons.append("missing_stop_anchor")
            elif not guarded.take_profit_plan or not guarded.invalidation_rule:
                guarded.trade_stage = TradeStage.FOCUS
                guarded.downgrade_reasons.append("execution_plan_incomplete")

        if _is_bottom_divergence_v2(candidate):
            self._apply_bottom_divergence_v2_guard(candidate, guarded)

        if _is_higher_than(guarded.trade_stage, candidate.trade_stage):
            guarded.trade_stage = candidate.trade_stage
            guarded.downgrade_reasons.append("rule_conflict")

        if (
            _is_bottom_divergence_v2(candidate)
            and guarded.trade_stage
            not in {TradeStage.PROBE_ENTRY, TradeStage.ADD_ON_STRENGTH}
        ):
            _clear_execution_advice(guarded)

        deduped: list[str] = []
        for reason in guarded.downgrade_reasons:
            if reason not in deduped:
                deduped.append(reason)
        guarded.downgrade_reasons = deduped
        guarded.ai_operation_advice = guarded.trade_stage.value
        return guarded

    @staticmethod
    def _apply_bottom_divergence_v2_guard(
        candidate: CandidateDecision,
        guarded: ScreeningAiReviewResult,
    ) -> None:
        if guarded.trade_stage not in {
            TradeStage.PROBE_ENTRY,
            TradeStage.ADD_ON_STRENGTH,
        }:
            return

        snapshot = candidate.factor_snapshot or {}
        stage = str(
            snapshot.get("bottom_divergence_v2_stage") or ""
        ).strip().lower()
        status = str(
            snapshot.get("bottom_divergence_v2_actionability_status") or ""
        ).strip().lower()

        allowed_statuses = _V2_ALLOWED_ACTIONABILITY_STATUSES.get(stage)
        if allowed_statuses is None:
            _downgrade_v2(guarded, "v2_non_actionable_stage")
            return
        if (
            stage == "major_actionable"
            and snapshot.get(
                "bottom_divergence_v2_major_actionable_entry"
            ) is not True
        ):
            _downgrade_v2(guarded, "v2_major_not_actionable")
            return
        if status not in allowed_statuses:
            reason = (
                "v2_adjustment_unknown"
                if status == "adjustment_unknown"
                else "v2_actionability_status_not_allowed"
            )
            _downgrade_v2(guarded, reason)
            return
        if resolve_current_stage_buy_point(snapshot) is None:
            _downgrade_v2(guarded, "v2_missing_current_buy_point")
            return
        if resolve_current_stage_stop_loss(snapshot) is None:
            _downgrade_v2(guarded, "v2_missing_current_stop")


def _is_higher_than(left: TradeStage, right: TradeStage) -> bool:
    return _STAGE_ORDER.get(left, -1) > _STAGE_ORDER.get(right, -1)


def _is_bottom_divergence_v2(candidate: CandidateDecision) -> bool:
    setup_type = getattr(candidate.setup_type, "value", candidate.setup_type)
    return setup_type == SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY.value


def _downgrade_v2(
    guarded: ScreeningAiReviewResult,
    reason: str,
) -> None:
    guarded.trade_stage = TradeStage.FOCUS
    guarded.downgrade_reasons.append(reason)


def _clear_execution_advice(guarded: ScreeningAiReviewResult) -> None:
    guarded.initial_position = None
    for field_name in _EXECUTION_ADVICE_FIELDS:
        if hasattr(guarded, field_name):
            setattr(guarded, field_name, None)

    if guarded.raw_payload is not None:
        guarded.raw_payload = dict(guarded.raw_payload)
        for field_name in _EXECUTION_ADVICE_FIELDS:
            if field_name in guarded.raw_payload:
                guarded.raw_payload[field_name] = None
        guarded.raw_payload["trade_stage"] = guarded.trade_stage.value
    guarded.raw_model_output = None
