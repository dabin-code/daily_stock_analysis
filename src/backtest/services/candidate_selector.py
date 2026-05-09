# -*- coding: utf-8 -*-
"""CandidateSelector: reads ScreeningCandidate records and extracts
five-layer snapshot fields for backtest evaluation.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Dict, List, Optional

from src.storage import DatabaseManager, ScreeningCandidate, ScreeningRun

logger = logging.getLogger(__name__)


def _loads_json(raw: Optional[str], default: Any) -> Any:
    if not raw:
        return default
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default
    return parsed if parsed is not None else default


def _normalize_json_field(
    raw: Optional[str],
    default: Any,
    expected_type: type | tuple[type, ...],
    field_name: str,
    candidate: ScreeningCandidate,
) -> Any:
    parsed = _loads_json(raw, default)
    if isinstance(parsed, expected_type):
        return parsed
    logger.warning(
        "Unexpected JSON shape for %s on screening candidate %s/%s; fallback to default",
        field_name,
        candidate.run_id,
        candidate.code,
    )
    return default


class CandidateSelector:
    """Reads screening candidates and returns dicts with five-layer fields."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    def select_candidates(self, screening_run_id: str) -> List[Dict[str, Any]]:
        """Select all candidates from a specific screening run."""
        with self.db.get_session() as session:
            run = (
                session.query(ScreeningRun)
                .filter(ScreeningRun.run_id == screening_run_id)
                .first()
            )
            if run is None:
                return []

            candidates = (
                session.query(ScreeningCandidate)
                .filter(ScreeningCandidate.run_id == screening_run_id)
                .order_by(ScreeningCandidate.rank)
                .all()
            )
            return [
                self._to_candidate_dict(c, run.trade_date)
                for c in candidates
            ]

    def select_candidates_by_date_range(
        self,
        date_from: date,
        date_to: date,
        market: str = "cn",
    ) -> List[Dict[str, Any]]:
        """Select candidates across all completed runs within a date range."""
        with self.db.get_session() as session:
            runs = (
                session.query(ScreeningRun)
                .filter(
                    ScreeningRun.trade_date >= date_from,
                    ScreeningRun.trade_date <= date_to,
                    ScreeningRun.market == market,
                    ScreeningRun.status.in_(["completed", "completed_with_ai_degraded"]),
                )
                .all()
            )
            results = []
            for run in runs:
                candidates = (
                    session.query(ScreeningCandidate)
                    .filter(ScreeningCandidate.run_id == run.run_id)
                    .order_by(ScreeningCandidate.rank)
                    .all()
                )
                results.extend(
                    self._to_candidate_dict(c, run.trade_date)
                    for c in candidates
                )
            return results

    @staticmethod
    def _to_candidate_dict(
        candidate: ScreeningCandidate,
        trade_date: date,
    ) -> Dict[str, Any]:
        trade_plan = _loads_json(candidate.trade_plan_json, None)
        factor_snapshot = _normalize_json_field(
            candidate.factor_snapshot_json,
            {},
            dict,
            "factor_snapshot_json",
            candidate,
        )
        matched_strategies = _normalize_json_field(
            candidate.matched_strategies_json,
            [],
            list,
            "matched_strategies_json",
            candidate,
        )
        rule_hits = _normalize_json_field(
            candidate.rule_hits_json,
            [],
            list,
            "rule_hits_json",
            candidate,
        )
        parsed_decision_payload = _loads_json(candidate.candidate_decision_json, {})
        decision_payload = parsed_decision_payload if isinstance(parsed_decision_payload, dict) else {}
        if parsed_decision_payload not in ({}, None) and not isinstance(parsed_decision_payload, dict):
            logger.warning(
                "Unexpected JSON shape for candidate_decision_json on screening candidate %s/%s; fallback to empty dict",
                candidate.run_id,
                candidate.code,
            )

        return {
            "screening_run_id": candidate.run_id,
            "screening_candidate_id": candidate.id,
            "trade_date": trade_date,
            "code": candidate.code,
            "name": candidate.name,
            "rank": candidate.rank,
            "rule_score": candidate.rule_score,
            # A4 lite: expose first-seen vs latest-seen timestamps so
            # backtest can detect candidates rewritten after the run started.
            "candidate_created_at": candidate.created_at,
            "candidate_updated_at": candidate.updated_at,
            # Five-layer snapshot fields
            "trade_stage": candidate.trade_stage,
            "setup_type": candidate.setup_type,
            "entry_maturity": candidate.entry_maturity,
            "market_regime": candidate.market_regime,
            "theme_position": candidate.theme_position,
            "candidate_pool_level": candidate.candidate_pool_level,
            "risk_level": candidate.risk_level,
            "trade_plan": trade_plan,
            "factor_snapshot": factor_snapshot,
            "matched_strategies": matched_strategies,
            "rule_hits": rule_hits,
            "primary_strategy": decision_payload.get("primary_strategy"),
            "contributing_strategies": decision_payload.get("contributing_strategies") or [],
            "strategy_scores": decision_payload.get("strategy_scores") or {},
            # AI override fields
            "ai_trade_stage": candidate.ai_trade_stage,
            "ai_confidence": candidate.ai_confidence,
        }
