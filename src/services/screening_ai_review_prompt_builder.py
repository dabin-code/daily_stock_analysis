from __future__ import annotations

import json
from typing import Any, Dict

from src.schemas.trading_types import CandidateDecision, SetupType


SCREENING_AI_REVIEW_PROMPT_VERSION = "screening_ai_review_v2"


class ScreeningAiReviewPromptBuilder:
    def build(self, candidate: CandidateDecision) -> str:
        setup_payload = {
            "setup_type": getattr(candidate.setup_type, "value", candidate.setup_type),
            "entry_maturity": getattr(candidate.entry_maturity, "value", candidate.entry_maturity),
            "setup_hit_reasons": list(candidate.setup_hit_reasons),
            "matched_strategies": list(candidate.matched_strategies),
        }
        if candidate.setup_type == SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY:
            setup_payload["bottom_divergence_v2_evidence"] = (
                self._bottom_divergence_v2_evidence(candidate.factor_snapshot)
            )

        payload = {
            "context": {
                "prompt_version": SCREENING_AI_REVIEW_PROMPT_VERSION,
                "task": "screening_second_pass_review",
            },
            "market": {
                "environment_ok": candidate.environment_ok,
                "market_regime": getattr(candidate.market_regime, "value", candidate.market_regime),
                "rule_trade_stage": getattr(candidate.trade_stage, "value", candidate.trade_stage),
                "risk_level": getattr(candidate.risk_level, "value", candidate.risk_level),
            },
            "theme": {
                "theme_tag": candidate.theme_tag,
                "theme_position": getattr(candidate.theme_position, "value", candidate.theme_position),
                "theme_score": candidate.theme_score,
                "sector_strength": candidate.sector_strength,
            },
            "stock": {
                "code": candidate.code,
                "name": candidate.name,
                "rank": candidate.rank,
                "factor_snapshot": self._compact_factor_snapshot(candidate.factor_snapshot),
            },
            "setup": setup_payload,
            "trade_plan": candidate.trade_plan.to_payload() if hasattr(candidate.trade_plan, "to_payload") else self._trade_plan_payload(candidate),
        }

        output_schema = {
            "environment_ok": "boolean",
            "trade_stage": "stand_aside|watch|focus|probe_entry|add_on_strength|reject",
            "entry_maturity": "low|medium|high",
            "setup_type": "bottom_divergence_breakout|bottom_divergence_layered_entry|low123_breakout|trend_breakout|trend_pullback|gap_breakout|limitup_structure|none",
            "risk_level": "low|medium|high",
            "initial_position": "string|null",
            "stop_loss_rule": "string|null",
            "take_profit_plan": "string|null",
            "invalidation_rule": "string|null",
            "reasoning_summary": "string",
            "confidence": "0.0~1.0",
        }

        return "\n".join(
            [
                f"prompt_version: {SCREENING_AI_REVIEW_PROMPT_VERSION}",
                "You are the screening AI second-pass review layer.",
                "Return JSON only.",
                "AI cannot override environment/theme hard constraints.",
                "If evidence is missing, missing evidence must downgrade conservatively.",
                "For bottom_divergence_layered_entry, Historical major_breakout does not mean the candidate is currently actionable.",
                "Missing R2 for an early-stage candidate is not insufficient evidence.",
                "If major_actionable_entry=false, stage is stale/extended/invalidated/major_unverified/breakout_failed, "
                "actionability_status is adjustment_unknown, or there is no current-stage buy point and stop, "
                "you must not return probe_entry or add_on_strength; return watch/focus/reject and explain the downgrade.",
                "Do not produce any UI/report wrapper.",
                "Keep the response strictly to the review schema.",
                "Input:",
                json.dumps(payload, ensure_ascii=False, indent=2),
                "Output schema:",
                json.dumps(output_schema, ensure_ascii=False, indent=2),
            ]
        )

    @staticmethod
    def _compact_factor_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
        compact: Dict[str, Any] = {}
        for key, value in (snapshot or {}).items():
            if (
                key == "bottom_divergence_v2_candidate_records"
                or key.lower().endswith("news")
                or key.lower().endswith("body")
            ):
                continue
            compact[key] = value
        return compact

    @staticmethod
    def _bottom_divergence_v2_evidence(snapshot: Dict[str, Any]) -> Dict[str, Any]:
        factors = snapshot or {}
        primary_record = ScreeningAiReviewPromptBuilder._primary_v2_record(
            factors
        )
        r1_touch_dates = ScreeningAiReviewPromptBuilder._v2_touch_dates(
            factors,
            primary_record,
            zone_key="r1",
            direct_keys=(
                "bottom_divergence_v2_near_zone_touch_dates",
                "bottom_divergence_v2_r1_touch_dates",
            ),
        )
        r2_touch_dates = ScreeningAiReviewPromptBuilder._v2_touch_dates(
            factors,
            primary_record,
            zone_key="r2",
            direct_keys=(
                "bottom_divergence_v2_major_zone_touch_dates",
                "bottom_divergence_v2_r2_touch_dates",
            ),
        )
        return {
            "stage": factors.get("bottom_divergence_v2_stage"),
            "pattern_code": factors.get("bottom_divergence_v2_pattern_code"),
            "early_strength": factors.get("bottom_divergence_v2_early_strength"),
            "r1": {
                "lower": factors.get("bottom_divergence_v2_near_zone_lower"),
                "upper": factors.get("bottom_divergence_v2_near_zone_upper"),
                "score": factors.get("bottom_divergence_v2_near_zone_score"),
                "touch_dates": r1_touch_dates,
            },
            "r2": {
                "lower": factors.get("bottom_divergence_v2_major_zone_lower"),
                "upper": factors.get("bottom_divergence_v2_major_zone_upper"),
                "score": factors.get("bottom_divergence_v2_major_zone_score"),
                "touch_dates": r2_touch_dates,
            },
            "candidate_version": factors.get(
                "bottom_divergence_v2_candidate_version"
            ),
            "zone_version": factors.get("bottom_divergence_v2_zone_version"),
            "degradation_reasons": factors.get(
                "bottom_divergence_v2_degradation_reasons"
            ),
            "major_breakout": factors.get(
                "bottom_divergence_v2_major_breakout"
            ),
            "major_actionable_entry": factors.get(
                "bottom_divergence_v2_major_actionable_entry"
            ),
            "actionability_status": factors.get(
                "bottom_divergence_v2_actionability_status"
            ),
            "event_days": factors.get("bottom_divergence_v2_event_days"),
        }

    @staticmethod
    def _primary_v2_record(factors: Dict[str, Any]) -> Dict[str, Any]:
        candidate_version = factors.get(
            "bottom_divergence_v2_candidate_version"
        )
        records = factors.get("bottom_divergence_v2_candidate_records")
        if candidate_version is None or not isinstance(records, list):
            return {}
        return next(
            (
                record
                for record in records
                if isinstance(record, dict)
                and record.get("candidate_version") == candidate_version
            ),
            {},
        )

    @staticmethod
    def _v2_touch_dates(
        factors: Dict[str, Any],
        primary_record: Dict[str, Any],
        *,
        zone_key: str,
        direct_keys: tuple[str, ...],
    ) -> list[str]:
        zone = primary_record.get("zone")
        zone_part = zone.get(zone_key) if isinstance(zone, dict) else None
        touch_points = (
            zone_part.get("touch_points")
            if isinstance(zone_part, dict)
            else None
        )
        dates = ScreeningAiReviewPromptBuilder._bounded_touch_dates(
            touch_points
        )
        if dates:
            return dates
        for key in direct_keys:
            if key in factors:
                return ScreeningAiReviewPromptBuilder._bounded_touch_dates(
                    factors.get(key)
                )
        return []

    @staticmethod
    def _bounded_touch_dates(values: Any) -> list[str]:
        if not isinstance(values, (list, tuple, set)):
            return []
        dates: set[str] = set()
        for item in values:
            raw_date = item.get("date") if isinstance(item, dict) else item
            if raw_date is None:
                continue
            if hasattr(raw_date, "isoformat"):
                text = str(raw_date.isoformat()).strip()
            else:
                text = str(raw_date).strip()
            if text:
                dates.add(text)
        return sorted(dates)[:5]

    @staticmethod
    def _trade_plan_payload(candidate: CandidateDecision) -> Dict[str, Any]:
        trade_plan = candidate.trade_plan
        if trade_plan is None:
            return {}
        return {
            "initial_position": getattr(trade_plan, "initial_position", None),
            "add_rule": getattr(trade_plan, "add_rule", None),
            "stop_loss_rule": getattr(trade_plan, "stop_loss_rule", None),
            "take_profit_plan": getattr(trade_plan, "take_profit_plan", None),
            "invalidation_rule": getattr(trade_plan, "invalidation_rule", None),
            "holding_expectation": getattr(trade_plan, "holding_expectation", None),
            "execution_note": getattr(trade_plan, "execution_note", None),
        }
