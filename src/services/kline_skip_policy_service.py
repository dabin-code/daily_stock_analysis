from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Set

from src.config import Config, get_config
from src.storage import DatabaseManager

logger = logging.getLogger(__name__)


class KlineSkipPolicyService:
    """Approve small, explainable K-line gaps without weakening hard failures."""

    def __init__(
        self,
        config: Optional[Config] = None,
        db_manager: Optional[DatabaseManager] = None,
    ) -> None:
        self.config = config or get_config()
        self.db = db_manager or DatabaseManager.get_instance()

    def apply_auto_skip(
        self,
        *,
        market: str,
        source_run_id: str,
        trade_date: date,
        sync_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not bool(getattr(self.config, "kline_audit_auto_skip_enabled", True)):
            return {"approved_count": 0, "skipped_reason": "disabled"}

        errors = list(sync_result.get("errors") or [])
        eligible_errors = self._eligible_errors(errors)
        if not eligible_errors:
            return {"approved_count": 0, "skipped_reason": "no_eligible_errors"}
        if len(eligible_errors) != len(errors):
            return {"approved_count": 0, "skipped_reason": "non_eligible_errors_present"}

        total = self._resolve_total(sync_result)
        eligible_codes = self._extract_codes(eligible_errors)
        if total <= 0:
            return {"approved_count": 0, "skipped_reason": "missing_total"}
        if len(eligible_codes) > int(getattr(self.config, "kline_audit_auto_skip_max_symbols", 20)):
            return {"approved_count": 0, "skipped_reason": "too_many_symbols"}

        ratio = len(eligible_codes) / total
        if ratio > float(getattr(self.config, "kline_audit_auto_skip_max_ratio", 0.005)):
            return {"approved_count": 0, "skipped_reason": "ratio_exceeded"}

        coverage = self._resolve_coverage(sync_result, total)
        if coverage < float(getattr(self.config, "kline_audit_auto_skip_min_coverage", 0.99)):
            return {"approved_count": 0, "skipped_reason": "coverage_below_threshold"}

        approved_count = 0
        gaps = self.db.list_kline_audit_gaps(
            market=market,
            gap_scope="symbol_range_gap",
        )
        for gap in gaps:
            if gap.status not in {"open", "pending_retry", "candidate_skip"}:
                continue
            if gap.source_run_id != source_run_id or str(gap.code or "").strip().upper() not in eligible_codes:
                continue
            if gap.missing_date_from != trade_date or gap.missing_date_to != trade_date:
                continue
            self.db.upsert_kline_skip_registry(
                market=market,
                gap_scope="symbol_range_gap",
                code=str(gap.code).strip().upper(),
                missing_date_from=gap.missing_date_from,
                missing_date_to=gap.missing_date_to,
                status="approved_skip",
                approved_by="system",
                approved_at=datetime.now(),
                reason_type="auto_small_skip_eligible_gap",
                notes=(
                    f"auto-approved small K-line gap: trade_date={trade_date.isoformat()} "
                    f"ratio={ratio:.6f} coverage={coverage:.6f}"
                ),
                success_streak=0,
            )
            self.db.upsert_kline_audit_gap(
                market=gap.market,
                gap_scope=gap.gap_scope,
                code=gap.code,
                trade_date=gap.trade_date,
                missing_date_from=gap.missing_date_from,
                missing_date_to=gap.missing_date_to,
                source_run_id=gap.source_run_id,
                status="approved_skip",
            )
            self.db.append_kline_audit_event(
                source_run_id=gap.source_run_id,
                gap_key=gap.gap_key,
                event_type="auto_approved_skip_granted",
                event_status="approved_skip",
                payload={
                    "approved_by": "system",
                    "reason_type": "auto_small_skip_eligible_gap",
                    "trade_date": trade_date.isoformat(),
                    "ratio": ratio,
                    "coverage": coverage,
                },
            )
            approved_count += 1
        self._mark_covered_market_day_gap_healthy(
            market=market,
            source_run_id=source_run_id,
            trade_date=trade_date,
        )

        logger.info(
            "K-line auto skip policy approved %s gaps for %s/%s",
            approved_count,
            market,
            trade_date,
        )
        return {
            "approved_count": approved_count,
            "eligible_symbol_count": len(eligible_codes),
            "ratio": ratio,
            "coverage": coverage,
        }

    def _mark_covered_market_day_gap_healthy(
        self,
        *,
        market: str,
        source_run_id: str,
        trade_date: date,
    ) -> None:
        current_symbol_gaps = [
            gap
            for gap in self.db.list_kline_audit_gaps(
                market=market,
                gap_scope="symbol_range_gap",
            )
            if gap.source_run_id == source_run_id
            and gap.missing_date_from is not None
            and gap.missing_date_to is not None
            and gap.missing_date_from <= trade_date <= gap.missing_date_to
        ]
        if not current_symbol_gaps or any(
            gap.status not in {"approved_skip", "healthy"} for gap in current_symbol_gaps
        ):
            return

        for gap in self.db.list_kline_audit_gaps(
            market=market,
            gap_scope="market_day_gap",
        ):
            if (
                gap.source_run_id == source_run_id
                and gap.trade_date == trade_date
                and gap.status in {"open", "pending_retry", "candidate_skip"}
            ):
                self.db.upsert_kline_audit_gap(
                    market=gap.market,
                    gap_scope=gap.gap_scope,
                    trade_date=gap.trade_date,
                    source_run_id=gap.source_run_id,
                    status="healthy",
                )
                self.db.append_kline_audit_event(
                    source_run_id=gap.source_run_id,
                    gap_key=gap.gap_key,
                    event_type="auto_skip_covered_market_day_gap",
                    event_status="healthy",
                    payload={"trade_date": trade_date.isoformat()},
                )

    def _eligible_errors(self, errors: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        allowed_classes = self._csv_set(
            getattr(self.config, "kline_audit_auto_skip_reason_classes", "skip_eligible")
        )
        allowed_reasons = self._csv_set(
            getattr(self.config, "kline_audit_auto_skip_reasons", "not_in_bulk_universe,empty_data")
        )
        eligible: List[Dict[str, Any]] = []
        for error in errors:
            reason_class = str(error.get("reason_class") or "").strip().lower()
            reason = str(error.get("reason") or "").strip().lower()
            if reason_class in allowed_classes and reason in allowed_reasons:
                eligible.append(error)
        return eligible

    @staticmethod
    def _extract_codes(errors: Iterable[Dict[str, Any]]) -> Set[str]:
        return {
            str(error.get("code") or "").strip().upper()
            for error in errors
            if str(error.get("code") or "").strip()
        }

    @staticmethod
    def _resolve_total(sync_result: Dict[str, Any]) -> int:
        health_report = sync_result.get("health_report") or {}
        total = health_report.get("expected_count", sync_result.get("total", 0))
        try:
            return int(total or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _resolve_coverage(sync_result: Dict[str, Any], total: int) -> float:
        health_report = sync_result.get("health_report") or {}
        if "success_rate" in health_report:
            try:
                return float(health_report.get("success_rate") or 0.0)
            except (TypeError, ValueError):
                return 0.0
        available = health_report.get("available_count")
        if available is None:
            missing_count = len(sync_result.get("errors") or [])
            available = max(0, total - missing_count)
        try:
            return float(available or 0) / total if total > 0 else 0.0
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _csv_set(value: Any) -> Set[str]:
        return {item.strip().lower() for item in str(value or "").split(",") if item.strip()}
