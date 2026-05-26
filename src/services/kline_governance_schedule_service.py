from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from src.config import Config, get_config
from src.core import trading_calendar
from src.services.kline_audit_service import KlineAuditService
from src.services.kline_repair_service import KlineRepairService
from src.services.kline_skip_policy_service import KlineSkipPolicyService
from src.services.market_data_sync_service import MarketDataSyncService
from src.storage import DatabaseManager

logger = logging.getLogger(__name__)

_GOVERNANCE_READY_TIME = time(17, 0)
_XCALS_AVAILABLE = trading_calendar._XCALS_AVAILABLE


class KlineGovernanceScheduleService:
    """Coordinate scheduled K-line sync/audit/repair workflows."""

    def __init__(
        self,
        config: Optional[Config] = None,
        market_data_sync_service: Optional[MarketDataSyncService] = None,
        audit_service: Optional[KlineAuditService] = None,
        repair_service: Optional[KlineRepairService] = None,
        skip_policy_service: Optional[KlineSkipPolicyService] = None,
        db_manager: Optional[DatabaseManager] = None,
    ) -> None:
        self.config = config or get_config()
        self.db = db_manager or DatabaseManager.get_instance()
        self.market_data_sync_service = market_data_sync_service or MarketDataSyncService(
            db_manager=self.db
        )
        self.audit_service = audit_service or KlineAuditService(db_manager=self.db)
        self.repair_service = repair_service or KlineRepairService(
            db_manager=self.db,
            retry_max_attempts=getattr(
                self.config,
                "kline_retry_max_attempts",
                3,
            ),
            candidate_failure_threshold=getattr(
                self.config,
                "kline_skip_candidate_failure_threshold",
                3,
            ),
        )
        self.skip_policy_service = skip_policy_service or KlineSkipPolicyService(
            config=self.config,
            db_manager=self.db,
        )

    def resolve_target_trade_date(
        self,
        *,
        trade_date: Optional[date] = None,
        market: str = "cn",
        now: Optional[datetime] = None,
    ) -> date:
        if trade_date is not None:
            return trade_date

        if not _XCALS_AVAILABLE:
            raise RuntimeError(
                "trading calendar unavailable; explicit trade_date is required for K-line governance"
            )

        exchange_code = trading_calendar.MARKET_EXCHANGE.get(market)
        timezone_name = trading_calendar.MARKET_TIMEZONE.get(market)
        if not exchange_code or not timezone_name:
            raise RuntimeError(f"unsupported market for K-line governance: {market}")

        market_now = self._resolve_market_now(now=now, timezone_name=timezone_name)
        session_date = market_now.date()
        try:
            calendar = trading_calendar.xcals.get_calendar(exchange_code)
            session = datetime(session_date.year, session_date.month, session_date.day)
            if not calendar.is_session(session):
                raise RuntimeError(
                    "non-trading day; explicit trade_date is required for K-line governance"
                )
            if market_now.time() >= _GOVERNANCE_READY_TIME:
                return session_date
            previous_session = calendar.previous_session(session)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                "trading calendar unavailable; explicit trade_date is required for K-line governance"
            ) from exc

        previous_trade_date = getattr(previous_session, "date", None)
        if callable(previous_trade_date):
            return previous_trade_date()
        if isinstance(previous_session, datetime):
            return previous_session.date()
        if isinstance(previous_session, date):
            return previous_session
        raise RuntimeError("failed to resolve target trade_date from trading calendar")

    def run_daily_governance(
        self,
        *,
        trade_date: Optional[date] = None,
        market: str = "cn",
    ) -> Dict[str, Any]:
        try:
            target_trade_date = self.resolve_target_trade_date(
                trade_date=trade_date,
                market=market,
            )
        except RuntimeError as exc:
            if trade_date is None and "non-trading day" in str(exc):
                return {
                    "trade_date": None,
                    "run_result": "skipped",
                    "pass_status": "skipped",
                    "reason": "non_trading_day",
                }
            raise
        window_start = self._compute_window_start(
            target_trade_date=target_trade_date,
            lookback_days=getattr(self.config, "kline_audit_lookback_days", 30),
        )

        sync_result = self.market_data_sync_service.sync_trade_date(
            trade_date=target_trade_date,
            force=False,
        )
        audit_result = self.audit_service.audit_trade_date(
            market=market,
            trade_date=target_trade_date,
            window_start=window_start,
            window_end=target_trade_date,
            run_type="daily",
            trigger_type="scheduled",
        )
        self._promote_current_run_gaps_for_repair(
            market=market,
            source_run_id=audit_result.get("run_id"),
        )
        repair_result = self.repair_service.repair_gaps(
            market=market,
            governance_run_succeeded=False,
            governance_run_id=audit_result.get("run_id"),
            included_statuses={"pending_retry", "candidate_skip"},
        )
        auto_skip_result = self.skip_policy_service.apply_auto_skip(
            market=market,
            source_run_id=audit_result.get("run_id"),
            trade_date=target_trade_date,
            sync_result=sync_result,
        )
        re_audit_result = self.audit_service.audit_trade_date(
            market=market,
            trade_date=target_trade_date,
            window_start=window_start,
            window_end=target_trade_date,
            run_type="daily_reaudit",
            trigger_type="scheduled",
        )
        approved_skip_recovery_result = self.repair_service.repair_gaps(
            market=market,
            governance_run_succeeded=re_audit_result.get("run_result") == "succeeded",
            governance_run_id=re_audit_result.get("run_id"),
            included_statuses={"approved_skip"},
        )

        return {
            "trade_date": target_trade_date,
            "run_result": re_audit_result.get("run_result"),
            "pass_status": re_audit_result.get("pass_status"),
            "sync_result": sync_result,
            "audit_result": audit_result,
            "repair_result": repair_result,
            "auto_skip_result": auto_skip_result,
            "re_audit_result": re_audit_result,
            "approved_skip_recovery_result": approved_skip_recovery_result,
        }

    def run_deep_audit(
        self,
        *,
        trade_date: Optional[date] = None,
        market: str = "cn",
    ) -> Dict[str, Any]:
        try:
            target_trade_date = self.resolve_target_trade_date(
                trade_date=trade_date,
                market=market,
            )
        except RuntimeError as exc:
            if trade_date is None and "non-trading day" in str(exc):
                return {
                    "trade_date": None,
                    "run_result": "skipped",
                    "pass_status": "skipped",
                    "reason": "non_trading_day",
                }
            raise
        window_start = self._compute_window_start(
            target_trade_date=target_trade_date,
            lookback_days=getattr(self.config, "kline_deep_audit_lookback_days", 365),
        )
        daily_truth_record = self._ensure_daily_governance_passed(
            market=market,
            trade_date=target_trade_date,
        )
        try:
            audit_result = self.audit_service.audit_trade_date(
                market=market,
                trade_date=target_trade_date,
                window_start=window_start,
                window_end=target_trade_date,
                run_type="deep_audit",
                trigger_type="scheduled",
            )
            candidate_skip_summary = self._recheck_candidate_skips(market=market)
            final_audit_result = audit_result
            if candidate_skip_summary["candidate_skip_rechecked"] > 0:
                final_audit_result = self.audit_service.audit_trade_date(
                    market=market,
                    trade_date=target_trade_date,
                    window_start=window_start,
                    window_end=target_trade_date,
                    run_type="deep_audit_reaudit",
                    trigger_type="scheduled",
                )
        finally:
            self._restore_trade_date_truth(daily_truth_record)

        return {
            "trade_date": target_trade_date,
            "run_result": final_audit_result.get("run_result"),
            "pass_status": final_audit_result.get("pass_status"),
            "audit_result": audit_result,
            "final_audit_result": final_audit_result,
            **candidate_skip_summary,
        }

    @staticmethod
    def _compute_window_start(*, target_trade_date: date, lookback_days: int) -> date:
        safe_lookback_days = max(1, int(lookback_days))
        return target_trade_date - timedelta(days=safe_lookback_days - 1)

    @staticmethod
    def _resolve_market_now(*, now: Optional[datetime], timezone_name: str) -> datetime:
        timezone = ZoneInfo(timezone_name)
        if now is None:
            return datetime.now(timezone)
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone)
        return now.astimezone(timezone)

    def _recheck_candidate_skips(self, *, market: str) -> Dict[str, int]:
        candidate_gaps = [
            gap
            for gap in self.db.list_kline_audit_gaps(
                market=market,
                status="candidate_skip",
            )
            if getattr(gap, "status", None) == "candidate_skip"
        ]
        recover_candidate_skip = getattr(self.repair_service, "_recover_candidate_skip", None)
        recovered_count = 0

        if recover_candidate_skip is None:
            logger.warning("repair service does not expose candidate skip recovery hook")
            return {
                "candidate_skip_rechecked": len(candidate_gaps),
                "candidate_skip_recovered_count": 0,
            }

        for gap in candidate_gaps:
            if recover_candidate_skip(gap):
                recovered_count += 1

        return {
            "candidate_skip_rechecked": len(candidate_gaps),
            "candidate_skip_recovered_count": recovered_count,
        }

    def _promote_current_run_gaps_for_repair(
        self,
        *,
        market: str,
        source_run_id: Optional[str],
    ) -> None:
        if not source_run_id:
            return

        for gap in self.db.list_kline_audit_gaps(market=market):
            if gap.source_run_id != source_run_id or gap.status != "open":
                continue
            self.db.upsert_kline_audit_gap(
                market=gap.market,
                gap_scope=gap.gap_scope,
                code=gap.code,
                trade_date=gap.trade_date,
                missing_date_from=gap.missing_date_from,
                missing_date_to=gap.missing_date_to,
                source_run_id=gap.source_run_id,
                status="pending_retry",
            )

    def _ensure_daily_governance_passed(self, *, market: str, trade_date: date) -> Any:
        audit_record = None
        if hasattr(self.db, "get_kline_audit_trade_date"):
            audit_record = self.db.get_kline_audit_trade_date(
                market=market,
                trade_date=trade_date,
            )
        if audit_record is None:
            raise RuntimeError(
                "daily governance truth is unavailable for deep audit: "
                f"market={market} trade_date={trade_date.isoformat()}"
            )

        pass_status = getattr(audit_record, "pass_status", None)
        normalized_status = str(pass_status or "").strip().lower()
        if normalized_status != "passed":
            raise RuntimeError(
                "daily governance truth is not passed for deep audit: "
                f"market={market} trade_date={trade_date.isoformat()} "
                f"pass_status={normalized_status or 'unknown'}"
            )
        return audit_record

    def _restore_trade_date_truth(self, audit_record: Any) -> None:
        if audit_record is None or not hasattr(self.db, "upsert_kline_audit_trade_date"):
            return

        def _field(name: str) -> Any:
            if isinstance(audit_record, dict):
                return audit_record.get(name)
            return getattr(audit_record, name, None)

        self.db.upsert_kline_audit_trade_date(
            market=_field("market"),
            trade_date=_field("trade_date"),
            pass_status=_field("pass_status"),
            window_start=_field("window_start"),
            window_end=_field("window_end"),
            rule_version=_field("rule_version"),
            source_run_id=_field("source_run_id"),
            passed_at=_field("passed_at"),
        )
