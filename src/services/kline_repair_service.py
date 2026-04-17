from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import select

from src.services.kline_audit_service import KlineAuditService
from src.services.market_data_sync_service import MarketDataSyncService
from src.storage import DatabaseManager, KlineAuditGap, StockDaily

logger = logging.getLogger(__name__)


class KlineRepairService:
    """执行 K 线补偿、候选豁免晋升与自动恢复。"""

    def __init__(
        self,
        db_manager: Optional[DatabaseManager] = None,
        sync_service: Optional[MarketDataSyncService] = None,
        candidate_failure_threshold: int = 2,
        retry_max_attempts: int = 3,
        approved_recovery_success_streak: int = 3,
    ) -> None:
        self.db = db_manager or DatabaseManager.get_instance()
        self.sync_service = sync_service or MarketDataSyncService(db_manager=self.db)
        self.candidate_failure_threshold = max(1, int(candidate_failure_threshold))
        self.retry_max_attempts = max(1, int(retry_max_attempts))
        self.approved_recovery_success_streak = max(1, int(approved_recovery_success_streak))

    def repair_gaps(
        self,
        *,
        market: str,
        governance_run_succeeded: bool,
        governance_run_id: Optional[str] = None,
        included_statuses: Optional[set[str]] = None,
    ) -> Dict[str, int]:
        repaired_gap_count = 0
        candidate_skip_gap_count = 0
        recovered_gap_count = 0
        target_statuses = included_statuses or {"pending_retry", "candidate_skip", "approved_skip"}

        unresolved_gaps = [
            gap
            for gap in self.db.list_kline_audit_gaps(market=market)
            if gap.status in target_statuses
        ]
        if governance_run_succeeded and governance_run_id is None:
            if any(gap.status == "approved_skip" for gap in unresolved_gaps):
                raise ValueError(
                    "governance_run_id is required when processing approved_skip recovery"
                )

        for gap in unresolved_gaps:
            if gap.status == "pending_retry":
                if self._has_reached_retry_limit(gap):
                    continue
                outcome = self._repair_pending_retry_gap(gap)
                if outcome == "healthy":
                    repaired_gap_count += 1
                    recovered_gap_count += 1
                elif outcome == "candidate_skip":
                    candidate_skip_gap_count += 1
                continue

            if gap.status == "candidate_skip":
                if self._recover_candidate_skip(gap):
                    recovered_gap_count += 1
                continue

            if gap.status == "approved_skip":
                if self._recover_approved_skip(
                    gap,
                    governance_run_succeeded=governance_run_succeeded,
                    governance_run_id=governance_run_id,
                ):
                    recovered_gap_count += 1

        return {
            "repaired_gap_count": repaired_gap_count,
            "candidate_skip_gap_count": candidate_skip_gap_count,
            "recovered_gap_count": recovered_gap_count,
        }

    def _has_reached_retry_limit(self, gap: KlineAuditGap) -> bool:
        attempt_events = self.db.list_kline_audit_events(
            gap_key=gap.gap_key,
            event_type="repair_attempted",
        )
        return len(attempt_events) >= self.retry_max_attempts

    def _repair_pending_retry_gap(self, gap: KlineAuditGap) -> str:
        repair_result = self._run_gap_repair(gap)
        self.db.append_kline_audit_event(
            source_run_id=gap.source_run_id,
            gap_key=gap.gap_key,
            event_type="repair_attempted",
            event_status="pending_retry",
            payload={
                "gap_scope": gap.gap_scope,
                "trade_date": gap.trade_date.isoformat() if gap.trade_date else None,
                "code": gap.code,
                "missing_date_from": (
                    gap.missing_date_from.isoformat() if gap.missing_date_from else None
                ),
                "missing_date_to": gap.missing_date_to.isoformat() if gap.missing_date_to else None,
                "error_count": len(repair_result["errors"]),
            },
        )

        if self._is_gap_repaired(gap):
            self._mark_gap_healthy(gap)
            return "healthy"

        if self._should_promote_candidate_skip(gap, repair_result["errors"]):
            self._promote_gap_to_candidate_skip(gap, repair_result["errors"])
            return "candidate_skip"

        return "pending_retry"

    def _run_gap_repair(self, gap: KlineAuditGap) -> Dict[str, object]:
        if gap.gap_scope == "market_day_gap":
            result = self.sync_service.sync_trade_date(
                trade_date=gap.trade_date,
                force=True,
            )
            return {"errors": list(result.get("errors", []))}

        if gap.gap_scope == "symbol_range_gap":
            errors: List[Dict[str, object]] = []
            for trade_date in self._iter_market_dates(
                market=gap.market,
                start=gap.missing_date_from,
                end=gap.missing_date_to,
            ):
                result = self.sync_service.sync_trade_date(
                    trade_date=trade_date,
                    stock_codes=[gap.code],
                    force=True,
                )
                errors.extend(result.get("errors", []))
            return {"errors": errors}

        raise ValueError(f"Unsupported gap scope: {gap.gap_scope}")

    def _should_promote_candidate_skip(
        self,
        gap: KlineAuditGap,
        errors: List[Dict[str, object]],
    ) -> bool:
        if not errors:
            return False

        attempt_events = self.db.list_kline_audit_events(
            gap_key=gap.gap_key,
            event_type="repair_attempted",
        )
        if len(attempt_events) < self.candidate_failure_threshold:
            return False

        source_names = {
            str(attempt.get("source"))
            for error in errors
            for attempt in list(error.get("source_attempts", []))
            if attempt.get("source")
        }
        if len(source_names) < 2:
            return False

        if not all(bool((error.get("candidate_skip") or {}).get("eligible")) for error in errors):
            return False

        registry_row = self._get_registry_row_for_gap(gap)
        return registry_row is None or registry_row.last_success_at is None

    def _promote_gap_to_candidate_skip(
        self,
        gap: KlineAuditGap,
        errors: List[Dict[str, object]],
    ) -> None:
        first_error = errors[0] if errors else {}
        self.db.upsert_kline_audit_gap(
            market=gap.market,
            gap_scope=gap.gap_scope,
            code=gap.code,
            trade_date=gap.trade_date,
            missing_date_from=gap.missing_date_from,
            missing_date_to=gap.missing_date_to,
            source_run_id=gap.source_run_id,
            status="candidate_skip",
        )
        self.db.upsert_kline_skip_registry(
            market=gap.market,
            gap_scope=gap.gap_scope,
            code=gap.code,
            trade_date=gap.trade_date,
            missing_date_from=gap.missing_date_from,
            missing_date_to=gap.missing_date_to,
            status="candidate_skip",
            reason_type=str(first_error.get("reason") or "candidate_skip"),
            notes="promoted by kline repair service",
        )
        self.db.append_kline_audit_event(
            source_run_id=gap.source_run_id,
            gap_key=gap.gap_key,
            event_type="promoted_to_candidate_skip",
            event_status="candidate_skip",
            payload={
                "reason_type": str(first_error.get("reason") or "candidate_skip"),
                "reason_class": str(first_error.get("reason_class") or "unknown"),
            },
        )

    def _recover_candidate_skip(self, gap: KlineAuditGap) -> bool:
        if not self._is_gap_repaired(gap):
            return False
        self._mark_gap_healthy(gap)
        return True

    def _recover_approved_skip(
        self,
        gap: KlineAuditGap,
        *,
        governance_run_succeeded: bool,
        governance_run_id: Optional[str],
    ) -> bool:
        registry_row = self._get_registry_row_for_gap(gap)
        if registry_row is None:
            return False

        if not self._is_gap_repaired(gap):
            if registry_row.success_streak:
                self.db.upsert_kline_skip_registry(
                    market=gap.market,
                    gap_scope=gap.gap_scope,
                    code=gap.code,
                    trade_date=gap.trade_date,
                    missing_date_from=gap.missing_date_from,
                    missing_date_to=gap.missing_date_to,
                    status="approved_skip",
                    success_streak=0,
                )
            return False

        if not governance_run_succeeded:
            if registry_row.success_streak:
                self.db.upsert_kline_skip_registry(
                    market=gap.market,
                    gap_scope=gap.gap_scope,
                    code=gap.code,
                    trade_date=gap.trade_date,
                    missing_date_from=gap.missing_date_from,
                    missing_date_to=gap.missing_date_to,
                    status="approved_skip",
                    success_streak=0,
                )
            return False

        if governance_run_id and self._has_recorded_governance_run(
            gap_key=gap.gap_key,
            governance_run_id=governance_run_id,
        ):
            return False

        new_streak = int(registry_row.success_streak or 0) + 1
        now = datetime.now()
        if new_streak >= self.approved_recovery_success_streak:
            self._mark_gap_healthy(
                gap,
                payload_extra={"governance_run_id": governance_run_id},
            )
            self.db.upsert_kline_skip_registry(
                market=gap.market,
                gap_scope=gap.gap_scope,
                code=gap.code,
                trade_date=gap.trade_date,
                missing_date_from=gap.missing_date_from,
                missing_date_to=gap.missing_date_to,
                status="healthy",
                success_streak=new_streak,
                last_success_at=now,
                last_recovered_at=now,
            )
            return True

        self.db.upsert_kline_skip_registry(
            market=gap.market,
            gap_scope=gap.gap_scope,
            code=gap.code,
            trade_date=gap.trade_date,
            missing_date_from=gap.missing_date_from,
            missing_date_to=gap.missing_date_to,
            status="approved_skip",
            success_streak=new_streak,
            last_success_at=now,
        )
        self.db.append_kline_audit_event(
            source_run_id=gap.source_run_id,
            gap_key=gap.gap_key,
            event_type="success_streak_incremented",
            event_status="approved_skip",
            payload={
                "success_streak": new_streak,
                "governance_run_id": governance_run_id,
            },
        )
        return False

    def _mark_gap_healthy(
        self,
        gap: KlineAuditGap,
        payload_extra: Optional[Dict[str, object]] = None,
    ) -> None:
        now = datetime.now()
        self.db.upsert_kline_audit_gap(
            market=gap.market,
            gap_scope=gap.gap_scope,
            code=gap.code,
            trade_date=gap.trade_date,
            missing_date_from=gap.missing_date_from,
            missing_date_to=gap.missing_date_to,
            source_run_id=gap.source_run_id,
            status="healthy",
        )

        registry_row = self._get_registry_row_for_gap(gap)
        if registry_row is not None:
            self.db.upsert_kline_skip_registry(
                market=gap.market,
                gap_scope=gap.gap_scope,
                code=gap.code,
                trade_date=gap.trade_date,
                missing_date_from=gap.missing_date_from,
                missing_date_to=gap.missing_date_to,
                status="healthy",
                success_streak=max(int(registry_row.success_streak or 0), 1),
                last_success_at=registry_row.last_success_at or now,
                last_recovered_at=now,
            )

        self.db.append_kline_audit_event(
            source_run_id=gap.source_run_id,
            gap_key=gap.gap_key,
            event_type="recovered",
            event_status="healthy",
            payload={
                "recovered_at": now.isoformat(),
                **(payload_extra or {}),
            },
        )

    def _get_registry_row_for_gap(self, gap: KlineAuditGap):
        return self.db.get_kline_skip_registry(
            market=gap.market,
            gap_scope=gap.gap_scope,
            code=gap.code,
            trade_date=gap.trade_date,
            missing_date_from=gap.missing_date_from,
            missing_date_to=gap.missing_date_to,
        )

    def _has_recorded_governance_run(
        self,
        *,
        gap_key: str,
        governance_run_id: str,
    ) -> bool:
        for event in self.db.list_kline_audit_events(gap_key=gap_key):
            if event.event_type not in {"success_streak_incremented", "recovered"}:
                continue
            payload = json.loads(event.payload_json) if event.payload_json else {}
            if payload.get("governance_run_id") == governance_run_id:
                return True
        return False

    def _is_gap_repaired(self, gap: KlineAuditGap) -> bool:
        if gap.gap_scope == "market_day_gap":
            instruments = self.db.list_instruments(
                market=gap.market,
                listing_status="active",
                exclude_st=gap.market == "cn",
            )
            expected_codes = [
                item["code"]
                for item in instruments
                if gap.trade_date is not None
                and (
                    self._coerce_date(item.get("list_date")) is None
                    or self._coerce_date(item.get("list_date")) <= gap.trade_date
                )
            ]
            if not expected_codes or gap.trade_date is None:
                return False
            available_codes = self.db.batch_has_today_data(expected_codes, target_date=gap.trade_date)
            return len(available_codes) == len(expected_codes)

        if gap.gap_scope == "symbol_range_gap":
            if not gap.code or gap.missing_date_from is None or gap.missing_date_to is None:
                return False
            expected_dates = self._iter_market_dates(
                market=gap.market,
                start=gap.missing_date_from,
                end=gap.missing_date_to,
            )
            if not expected_dates:
                return False
            with self.db.session_scope() as session:
                rows = session.execute(
                    select(StockDaily.date).where(
                        StockDaily.code == gap.code,
                        StockDaily.date >= expected_dates[0],
                        StockDaily.date <= expected_dates[-1],
                    )
                ).scalars().all()
            return set(rows) >= set(expected_dates)

        return False

    @staticmethod
    def _iter_market_dates(*, market: str, start: date, end: date) -> List[date]:
        dates: List[date] = []
        current = start
        while current <= end:
            if KlineAuditService._is_market_session(market=market, trade_date=current):
                dates.append(current)
            current += timedelta(days=1)
        return dates

    @staticmethod
    def _coerce_date(value: object) -> Optional[date]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            return date.fromisoformat(value)
        raise TypeError(f"Unsupported date value: {value!r}")
