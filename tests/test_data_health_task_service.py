from datetime import date
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.services.data_health_task_service import DataHealthTaskService


class _BackfillService:
    def __init__(self) -> None:
        self.calls = []

    def backfill_to_trade_date(self, target_trade_date: date, market: str):
        self.calls.append({"target_trade_date": target_trade_date, "market": market})
        return {
            "status": "completed",
            "target_trade_date": target_trade_date.isoformat(),
            "saved_rows": 10,
            "failed_dates": [],
            "governance_result": {"pass_status": "passed"},
        }


class _GovernanceService:
    def __init__(self) -> None:
        self.daily_calls = []
        self.deep_calls = []
        self.config = SimpleNamespace(kline_audit_lookback_days=30)

    def run_daily_governance(self, *, trade_date: date, market: str):
        self.daily_calls.append({"trade_date": trade_date, "market": market})
        return {"run_result": "succeeded", "pass_status": "passed", "trade_date": trade_date}

    def run_deep_audit(self, *, trade_date: date, market: str):
        self.deep_calls.append({"trade_date": trade_date, "market": market})
        return {"run_result": "succeeded", "pass_status": "passed", "trade_date": trade_date}


class _AuditService:
    def __init__(self) -> None:
        self.calls = []

    def audit_trade_date(self, **kwargs):
        self.calls.append(kwargs)
        return {"run_result": "succeeded", "pass_status": "passed", "trade_date": kwargs["trade_date"]}


class _RepairService:
    def __init__(self) -> None:
        self.calls = []

    def repair_gaps(self, **kwargs):
        self.calls.append(kwargs)
        return {"repaired_gap_count": 2, "candidate_skip_gap_count": 0, "recovered_gap_count": 2}


def _fake_db():
    db = MagicMock()
    db.list_kline_audit_gaps.return_value = []
    return db


def _build_service() -> tuple[DataHealthTaskService, _BackfillService, _GovernanceService, _RepairService, _AuditService]:
    backfill = _BackfillService()
    governance = _GovernanceService()
    repair = _RepairService()
    audit = _AuditService()
    service = DataHealthTaskService(
        backfill_service=backfill,
        governance_service=governance,
        repair_service=repair,
        audit_service=audit,
        db_manager=_fake_db(),
        run_inline=True,
    )
    return service, backfill, governance, repair, audit


def test_data_health_task_runs_backfill_operation_and_keeps_result():
    service, backfill, _governance, _repair, _audit = _build_service()

    task = service.submit_operation(
        operation_type="backfill_to_date",
        trade_date=date(2026, 5, 12),
        market="cn",
    )

    stored = service.get_task(task["task_id"])
    assert task["status"] == "completed"
    assert stored["status"] == "completed"
    assert stored["progress"] == 100
    assert stored["result"]["saved_rows"] == 10
    assert backfill.calls == [{"target_trade_date": date(2026, 5, 12), "market": "cn"}]


def test_data_health_task_dispatches_repair_and_audit_operations():
    service, _backfill, governance, repair, audit = _build_service()

    repair_task = service.submit_operation(operation_type="repair_gaps", market="cn")
    audit_task = service.submit_operation(
        operation_type="rerun_audit",
        trade_date=date(2026, 5, 12),
        market="cn",
    )

    assert repair_task["result"]["repaired_gap_count"] == 2
    assert repair.calls == [
        {
            "market": "cn",
            "governance_run_succeeded": False,
            "included_statuses": {"pending_retry", "candidate_skip"},
        }
    ]
    assert audit_task["result"]["pass_status"] == "passed"
    assert audit.calls == [
        {
            "market": "cn",
            "trade_date": date(2026, 5, 12),
            "window_start": date(2026, 4, 13),
            "window_end": date(2026, 5, 12),
            "run_type": "manual_audit",
            "trigger_type": "manual",
        }
    ]
    assert governance.daily_calls == []
    assert governance.deep_calls == []


def test_data_health_task_marks_failed_operation():
    service = DataHealthTaskService(
        backfill_service=SimpleNamespace(
            backfill_to_trade_date=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
        ),
        db_manager=_fake_db(),
        run_inline=True,
    )

    task = service.submit_operation(
        operation_type="backfill_to_date",
        trade_date=date(2026, 5, 12),
        market="cn",
    )

    assert task["status"] == "failed"
    assert task["progress"] == 100
    assert "boom" in task["error"]


def test_data_health_task_rejects_unknown_operation():
    service, _backfill, _governance, _repair, _audit = _build_service()

    with pytest.raises(ValueError, match="unsupported operation"):
        service.submit_operation(operation_type="unknown", market="cn")


def test_retry_failed_rejects_trade_date_without_explicit_stock_codes():
    service, _backfill, _governance, _repair, _audit = _build_service()

    with pytest.raises(ValueError, match="stock_codes"):
        service.submit_operation(
            operation_type="retry_failed",
            trade_date=date(2026, 5, 12),
            market="cn",
        )


def test_data_health_task_uses_injected_db_for_default_audit_service():
    db = _fake_db()
    db._engine = SimpleNamespace(
        url=SimpleNamespace(drivername="sqlite", database="isolated-test.db")
    )
    service = DataHealthTaskService(
        governance_service=_GovernanceService(),
        repair_service=_RepairService(),
        sync_service=MagicMock(),
        db_manager=db,
        run_inline=True,
    )

    assert service.db is db
    assert service.audit_service.db is db
    assert service.backfill_service.governance_service is service.governance_service
    assert service.backfill_service.db_path == "isolated-test.db"


def test_repair_gaps_promotes_open_gaps_before_repair():
    repair = _RepairService()
    db = MagicMock()
    db.list_kline_audit_gaps.return_value = [
        SimpleNamespace(
            market="cn",
            gap_scope="symbol_range_gap",
            code="000001",
            trade_date=None,
            missing_date_from=date(2026, 5, 8),
            missing_date_to=date(2026, 5, 11),
            source_run_id="audit-open",
            status="open",
        )
    ]
    service = DataHealthTaskService(
        backfill_service=_BackfillService(),
        repair_service=repair,
        db_manager=db,
        run_inline=True,
    )

    service.submit_operation(operation_type="repair_gaps", market="cn")

    db.upsert_kline_audit_gap.assert_called_once_with(
        market="cn",
        gap_scope="symbol_range_gap",
        code="000001",
        trade_date=None,
        missing_date_from=date(2026, 5, 8),
        missing_date_to=date(2026, 5, 11),
        source_run_id="audit-open",
        status="pending_retry",
    )


def test_inflight_dedup_keeps_different_operation_scopes_distinct():
    started = Event()
    release = Event()

    class _BlockingBackfillService:
        def backfill_to_trade_date(self, **kwargs):
            started.set()
            release.wait(timeout=5)
            return {"status": "completed", "target_trade_date": kwargs["target_trade_date"].isoformat()}

    service = DataHealthTaskService(
        backfill_service=_BlockingBackfillService(),
        db_manager=_fake_db(),
        run_inline=False,
    )

    first = service.submit_operation(
        operation_type="backfill_to_date",
        trade_date=date(2026, 5, 12),
        market="cn",
    )
    assert started.wait(timeout=2)
    second = service.submit_operation(
        operation_type="backfill_to_date",
        trade_date=date(2026, 5, 13),
        market="cn",
    )

    assert first["task_id"] != second["task_id"]
    release.set()
    service.shutdown()
