from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional

import logging

from src.storage import DatabaseManager
from src.services.fast_backfill_service import FastBackfillService
from src.services.kline_audit_service import KlineAuditService
from src.services.kline_governance_schedule_service import KlineGovernanceScheduleService
from src.services.kline_repair_service import KlineRepairService
from src.services.market_data_sync_service import MarketDataSyncService

logger = logging.getLogger(__name__)


class DataHealthTaskStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class DataHealthTask:
    task_id: str
    operation_type: str
    operation_key: str
    market: str = "cn"
    status: DataHealthTaskStatus = DataHealthTaskStatus.PENDING
    progress: int = 0
    message: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "operation_type": self.operation_type,
            "operation_key": self.operation_key,
            "market": self.market,
            "status": self.status.value,
            "progress": self.progress,
            "message": self.message,
            "result": _json_ready(self.result),
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class DataHealthTaskService:
    """In-process background task runner for data health operations."""

    _SUPPORTED_OPERATIONS = {
        "backfill_to_date",
        "repair_gaps",
        "rerun_audit",
        "retry_failed",
    }

    def __init__(
        self,
        *,
        backfill_service: Optional[Any] = None,
        governance_service: Optional[Any] = None,
        audit_service: Optional[Any] = None,
        repair_service: Optional[Any] = None,
        sync_service: Optional[Any] = None,
        db_manager: Optional[Any] = None,
        max_workers: int = 1,
        max_tasks: int = 100,
        run_inline: bool = False,
    ) -> None:
        self.db = (
            db_manager
            or getattr(repair_service, "db", None)
            or getattr(governance_service, "db", None)
            or getattr(sync_service, "db", None)
            or DatabaseManager.get_instance()
        )
        self.governance_service = governance_service or KlineGovernanceScheduleService(
            db_manager=self.db
        )
        self.backfill_service = backfill_service or FastBackfillService(
            db_path=self._resolve_backfill_db_path(self.db),
            governance_service=self.governance_service
        )
        self.repair_service = repair_service or KlineRepairService(db_manager=self.db)
        self.sync_service = sync_service or MarketDataSyncService(db_manager=self.db)
        self.audit_service = (
            audit_service
            or KlineAuditService(db_manager=self.db)
        )
        self.run_inline = run_inline
        self.max_tasks = max(1, int(max_tasks))
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="data_health_task_",
        )
        self._tasks: Dict[str, DataHealthTask] = {}
        self._lock = threading.RLock()

    def submit_operation(
        self,
        *,
        operation_type: str,
        market: str = "cn",
        trade_date: Optional[date] = None,
        stock_codes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        if operation_type not in self._SUPPORTED_OPERATIONS:
            raise ValueError(f"unsupported operation: {operation_type}")
        self._validate_operation_payload(
            operation_type=operation_type,
            trade_date=trade_date,
            stock_codes=stock_codes,
        )

        task = DataHealthTask(
            task_id=f"data-health-{uuid.uuid4().hex[:12]}",
            operation_type=operation_type,
            operation_key=self._build_operation_key(
                operation_type=operation_type,
                market=market,
                trade_date=trade_date,
                stock_codes=stock_codes,
            ),
            market=market,
            message="等待执行",
        )
        payload = {
            "operation_type": operation_type,
            "market": market,
            "trade_date": trade_date,
            "stock_codes": list(stock_codes or []),
        }
        with self._lock:
            self._reject_if_queue_full()
            existing_task = self._find_inflight_task_locked(
                operation_key=task.operation_key,
            )
            if existing_task is not None:
                return existing_task.to_dict()
            self._tasks[task.task_id] = task

        if self.run_inline:
            self._run_task(task.task_id, payload)
        else:
            self._executor.submit(self._run_task, task.task_id, payload)
        return self.get_task(task.task_id)

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            task = self._tasks.get(task_id)
            return task.to_dict() if task else None

    def list_tasks(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        with self._lock:
            tasks = sorted(self._tasks.values(), key=lambda item: item.created_at, reverse=True)
            return [task.to_dict() for task in tasks[:safe_limit]]

    def _run_task(self, task_id: str, payload: Dict[str, Any]) -> None:
        self._mark_processing(task_id)
        try:
            result = self._dispatch_operation(payload)
        except Exception as exc:  # pragma: no cover - exact exception varies by provider
            logger.exception(
                "data health task failed: task_id=%s operation=%s market=%s",
                task_id,
                payload.get("operation_type"),
                payload.get("market"),
            )
            self._mark_failed(task_id, str(exc) or exc.__class__.__name__)
            return
        self._mark_completed(task_id, result)

    def _dispatch_operation(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        operation_type = payload["operation_type"]
        market = payload["market"]
        trade_date = payload.get("trade_date")
        stock_codes = payload.get("stock_codes") or []

        if operation_type == "backfill_to_date":
            self._require_trade_date(trade_date, operation_type)
            return self.backfill_service.backfill_to_trade_date(
                target_trade_date=trade_date,
                market=market,
            )

        if operation_type == "rerun_audit":
            self._require_trade_date(trade_date, operation_type)
            window_start = KlineGovernanceScheduleService._compute_window_start(
                target_trade_date=trade_date,
                lookback_days=getattr(
                    getattr(self.governance_service, "config", None),
                    "kline_audit_lookback_days",
                    30,
                ),
            )
            return self.audit_service.audit_trade_date(
                market=market,
                trade_date=trade_date,
                window_start=window_start,
                window_end=trade_date,
                run_type="manual_audit",
                trigger_type="manual",
            )

        if operation_type == "repair_gaps":
            self._promote_open_gaps_for_repair(market=market)
            repair_result = self.repair_service.repair_gaps(
                market=market,
                governance_run_succeeded=False,
                included_statuses={"pending_retry", "candidate_skip"},
                max_trade_date=self._resolve_repair_max_trade_date(market=market),
            )
            # 修复缺口本身只补数据/标记 healthy，不会翻转审计通过状态。
            # 逐日补跑治理，让每个交易日各自获得当日豁免证据，把审计通过日推进到最新日，
            # 否则最新交易日始终 not_passed，选股拿不到可用交易日。
            catch_up_result = self._run_catch_up_governance(market=market)
            return {
                "repair_result": repair_result,
                "catch_up_result": catch_up_result,
            }

        if operation_type == "retry_failed":
            if trade_date is not None:
                sync_result = self.sync_service.sync_trade_date(
                    trade_date=trade_date,
                    stock_codes=stock_codes,
                    force=True,
                )
                catch_up_result = self._run_catch_up_governance(market=market)
                return {
                    "sync_result": sync_result,
                    "catch_up_result": catch_up_result,
                }
            self._promote_open_gaps_for_repair(market=market)
            repair_result = self.repair_service.repair_gaps(
                market=market,
                governance_run_succeeded=False,
                included_statuses={"pending_retry", "candidate_skip"},
                max_trade_date=self._resolve_repair_max_trade_date(market=market),
            )
            catch_up_result = self._run_catch_up_governance(market=market)
            return {
                "repair_result": repair_result,
                "catch_up_result": catch_up_result,
            }

        raise ValueError(f"unsupported operation: {operation_type}")

    def _run_catch_up_governance(self, *, market: str) -> Optional[Dict[str, Any]]:
        """逐日补跑治理以推进审计通过日；治理服务不支持或失败时不影响修复结果。"""
        catch_up = getattr(self.governance_service, "run_daily_governance_with_catch_up", None)
        if not callable(catch_up):
            return None
        try:
            return catch_up(market=market)
        except Exception as exc:  # 补跑失败不应吞掉已完成的修复结果
            logger.warning("catch-up governance after repair failed: market=%s error=%s", market, exc)
            return {"status": "catch_up_failed", "error": str(exc) or exc.__class__.__name__}

    def _mark_processing(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task.status = DataHealthTaskStatus.PROCESSING
            task.progress = 10
            task.message = "执行中"
            task.started_at = datetime.now()

    def _mark_completed(self, task_id: str, result: Dict[str, Any]) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task.status = DataHealthTaskStatus.COMPLETED
            task.progress = 100
            task.message = "已完成"
            task.result = result
            task.completed_at = datetime.now()

    def _mark_failed(self, task_id: str, error: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task.status = DataHealthTaskStatus.FAILED
            task.progress = 100
            task.message = "执行失败"
            task.error = error
            task.completed_at = datetime.now()

    @staticmethod
    def _require_trade_date(value: Optional[date], operation_type: str) -> None:
        if value is None:
            raise ValueError(f"{operation_type} requires trade_date")

    @staticmethod
    def _resolve_backfill_db_path(db_manager: Any) -> Optional[str]:
        engine = getattr(db_manager, "_engine", None)
        url = getattr(engine, "url", None)
        drivername = getattr(url, "drivername", None)
        database = getattr(url, "database", None)
        if not isinstance(drivername, str) or not drivername.startswith("sqlite"):
            return None
        return str(database) if database else None

    @staticmethod
    def _validate_operation_payload(
        *,
        operation_type: str,
        trade_date: Optional[date],
        stock_codes: Optional[List[str]],
    ) -> None:
        if operation_type == "retry_failed" and trade_date is not None and not stock_codes:
            raise ValueError("retry_failed with trade_date requires explicit stock_codes")
        if stock_codes and len(stock_codes) > 200:
            raise ValueError("stock_codes cannot exceed 200 items")

    def _promote_open_gaps_for_repair(self, *, market: str) -> None:
        open_gaps = self.db.list_kline_audit_gaps(market=market, status="open")
        for gap in open_gaps:
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

    def _resolve_repair_max_trade_date(self, *, market: str) -> Optional[date]:
        if not hasattr(self.db, "get_latest_passed_kline_audit_trade_date"):
            return None
        latest_passed = self.db.get_latest_passed_kline_audit_trade_date(market=market)
        if latest_passed is None:
            return None
        trade_date = latest_passed.get("trade_date") if isinstance(latest_passed, dict) else getattr(latest_passed, "trade_date", None)
        return trade_date if isinstance(trade_date, date) else None

    def _reject_if_queue_full(self) -> None:
        active_count = sum(
            1
            for task in self._tasks.values()
            if task.status in (DataHealthTaskStatus.PENDING, DataHealthTaskStatus.PROCESSING)
        )
        if active_count >= self.max_tasks:
            raise ValueError("data health task queue is full")

    def _find_inflight_task_locked(
        self,
        *,
        operation_key: str,
    ) -> Optional[DataHealthTask]:
        for task in self._tasks.values():
            if task.status not in (DataHealthTaskStatus.PENDING, DataHealthTaskStatus.PROCESSING):
                continue
            if task.operation_key == operation_key:
                return task
        return None

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)

    @staticmethod
    def _build_operation_key(
        *,
        operation_type: str,
        market: str,
        trade_date: Optional[date],
        stock_codes: Optional[List[str]],
    ) -> str:
        date_part = trade_date.isoformat() if trade_date else ""
        codes_part = ",".join(sorted(stock_codes or []))
        return "|".join([operation_type, market, date_part, codes_part])


def _json_ready(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(item) for item in value]
    return value


_DEFAULT_TASK_SERVICE: Optional[DataHealthTaskService] = None
_DEFAULT_TASK_SERVICE_LOCK = threading.Lock()


def get_data_health_task_service() -> DataHealthTaskService:
    global _DEFAULT_TASK_SERVICE
    if _DEFAULT_TASK_SERVICE is None:
        with _DEFAULT_TASK_SERVICE_LOCK:
            if _DEFAULT_TASK_SERVICE is None:
                _DEFAULT_TASK_SERVICE = DataHealthTaskService()
    return _DEFAULT_TASK_SERVICE
