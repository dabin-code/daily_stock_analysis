import os
import tempfile
from datetime import date

import pandas as pd

from src.config import Config
from src.storage import DatabaseManager


def _build_daily_rows(trade_dates: list[date], close: float = 10.0) -> pd.DataFrame:
    rows = []
    for idx, trade_date in enumerate(trade_dates):
        price = close + idx * 0.1
        rows.append(
            {
                "date": trade_date,
                "open": price - 0.1,
                "high": price + 0.1,
                "low": price - 0.2,
                "close": price,
                "volume": 1000 + idx * 10,
                "amount": (1000 + idx * 10) * price,
                "pct_chg": 1.0,
            }
        )
    return pd.DataFrame(rows)


class _StubSyncService:
    def __init__(self, results, on_call=None):
        self._results = list(results)
        self._on_call = on_call
        self.calls = []

    def sync_trade_date(self, trade_date, stock_codes=None, force=False, **kwargs):
        self.calls.append(
            {
                "trade_date": trade_date,
                "stock_codes": list(stock_codes) if stock_codes else None,
                "force": force,
                "kwargs": kwargs,
            }
        )
        if self._on_call is not None:
            self._on_call(trade_date=trade_date, stock_codes=stock_codes, force=force, **kwargs)
        return self._results.pop(0)


def _seed_active_instruments(db: DatabaseManager) -> None:
    db.upsert_instruments(
        [
            {
                "code": "000001",
                "name": "Ping An Bank",
                "market": "cn",
                "exchange": "SZSE",
                "listing_status": "active",
                "is_st": False,
                "industry": "Bank",
                "list_date": date(2000, 1, 1),
            },
            {
                "code": "000002",
                "name": "Vanke",
                "market": "cn",
                "exchange": "SZSE",
                "listing_status": "active",
                "is_st": False,
                "industry": "Property",
                "list_date": date(2000, 1, 1),
            },
        ]
    )


def _create_run(db: DatabaseManager, run_id: str) -> None:
    db.create_kline_audit_run(
        run_id=run_id,
        market="cn",
        trade_date=date(2026, 4, 16),
        run_type="daily",
        trigger_type="manual",
        run_result="degraded",
        pass_status="not_passed",
        rule_version="kline_audit_v1",
        window_start=date(2026, 4, 10),
        window_end=date(2026, 4, 16),
    )


def test_repair_service_retries_pending_retry_gap_and_records_event():
    from src.services.kline_repair_service import KlineRepairService

    temp_dir = tempfile.TemporaryDirectory()
    try:
        db_path = os.path.join(temp_dir.name, "test_kline_repair_service.db")
        os.environ["DATABASE_PATH"] = db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()
        db = DatabaseManager.get_instance()
        _seed_active_instruments(db)
        _create_run(db, "repair-run-001")
        gap = db.upsert_kline_audit_gap(
            market="cn",
            gap_scope="market_day_gap",
            trade_date=date(2026, 4, 16),
            source_run_id="repair-run-001",
            status="pending_retry",
        )

        def _repair_market_day(**_kwargs):
            db.save_daily_data(_build_daily_rows([date(2026, 4, 16)]), "000001", data_source="test")
            db.save_daily_data(_build_daily_rows([date(2026, 4, 16)]), "000002", data_source="test")

        sync_service = _StubSyncService(
            [{"trade_date": "2026-04-16", "errors": [], "health_report": {"is_healthy": True}}],
            on_call=_repair_market_day,
        )
        service = KlineRepairService(db_manager=db, sync_service=sync_service, candidate_failure_threshold=2)

        result = service.repair_gaps(market="cn", governance_run_succeeded=False)

        refreshed_gap = db.list_kline_audit_gaps(market="cn", status="healthy")[0]
        events = db.list_kline_audit_events(gap_key=gap.gap_key)

        assert result["repaired_gap_count"] == 1
        assert refreshed_gap.gap_key == gap.gap_key
        assert sync_service.calls == [
            {"trade_date": date(2026, 4, 16), "stock_codes": None, "force": True, "kwargs": {}}
        ]
        assert [event.event_type for event in events] == ["repair_attempted", "recovered"]
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        temp_dir.cleanup()


def test_repair_service_stops_retrying_pending_gap_after_retry_limit():
    from src.services.kline_repair_service import KlineRepairService

    temp_dir = tempfile.TemporaryDirectory()
    try:
        db_path = os.path.join(temp_dir.name, "test_kline_repair_service_retry_limit.db")
        os.environ["DATABASE_PATH"] = db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()
        db = DatabaseManager.get_instance()
        _create_run(db, "repair-run-retry-limit")
        gap = db.upsert_kline_audit_gap(
            market="cn",
            gap_scope="symbol_range_gap",
            code="000001",
            missing_date_from=date(2026, 4, 10),
            missing_date_to=date(2026, 4, 11),
            source_run_id="repair-run-retry-limit",
            status="pending_retry",
        )
        for attempt in range(3):
            db.append_kline_audit_event(
                gap_key=gap.gap_key,
                source_run_id="repair-run-retry-limit",
                event_type="repair_attempted",
                event_status="pending_retry",
                payload={"attempt": attempt + 1},
            )

        sync_service = _StubSyncService([])
        service = KlineRepairService(
            db_manager=db,
            sync_service=sync_service,
            candidate_failure_threshold=2,
            retry_max_attempts=3,
        )

        result = service.repair_gaps(market="cn", governance_run_succeeded=False)

        pending_retry_gaps = db.list_kline_audit_gaps(market="cn", status="pending_retry")
        events = db.list_kline_audit_events(gap_key=gap.gap_key, event_type="repair_attempted")

        assert result == {
            "repaired_gap_count": 0,
            "candidate_skip_gap_count": 0,
            "recovered_gap_count": 0,
            "skipped_gap_count": 0,
        }
        assert len(sync_service.calls) == 0
        assert len(pending_retry_gaps) == 1
        assert pending_retry_gaps[0].gap_key == gap.gap_key
        assert len(events) == 3
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        temp_dir.cleanup()


def test_repair_service_promotes_gap_to_candidate_skip_after_threshold():
    from src.services.kline_repair_service import KlineRepairService

    temp_dir = tempfile.TemporaryDirectory()
    try:
        db_path = os.path.join(temp_dir.name, "test_kline_repair_service.db")
        os.environ["DATABASE_PATH"] = db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()
        db = DatabaseManager.get_instance()
        _create_run(db, "repair-run-002")
        gap = db.upsert_kline_audit_gap(
            market="cn",
            gap_scope="symbol_range_gap",
            code="000001",
            missing_date_from=date(2026, 4, 10),
            missing_date_to=date(2026, 4, 11),
            source_run_id="repair-run-002",
            status="pending_retry",
        )
        db.append_kline_audit_event(
            gap_key=gap.gap_key,
            source_run_id="repair-run-002",
            event_type="repair_attempted",
            event_status="pending_retry",
            payload={"attempt": 1},
        )

        sync_service = _StubSyncService(
            [
                {
                    "trade_date": "2026-04-10",
                    "errors": [
                        {
                            "code": "000001",
                            "reason": "empty_data",
                            "reason_class": "skip_eligible",
                            "source_attempts": [
                                {"source": "AkShareFetcher", "reason_class": "skip_eligible"},
                                {"source": "TushareFetcher", "reason_class": "skip_eligible"},
                            ],
                            "candidate_skip": {
                                "eligible": True,
                                "total_attempts": 2,
                                "skip_eligible_attempts": 2,
                                "retryable_attempts": 0,
                                "blocking_attempts": 0,
                            },
                        }
                    ],
                    "health_report": {"is_healthy": False},
                },
                {
                    "trade_date": "2026-04-11",
                    "errors": [
                        {
                            "code": "000001",
                            "reason": "empty_data",
                            "reason_class": "skip_eligible",
                            "source_attempts": [
                                {"source": "AkShareFetcher", "reason_class": "skip_eligible"},
                                {"source": "TushareFetcher", "reason_class": "skip_eligible"},
                            ],
                            "candidate_skip": {
                                "eligible": True,
                                "total_attempts": 2,
                                "skip_eligible_attempts": 2,
                                "retryable_attempts": 0,
                                "blocking_attempts": 0,
                            },
                        }
                    ],
                    "health_report": {"is_healthy": False},
                },
            ]
        )
        service = KlineRepairService(db_manager=db, sync_service=sync_service, candidate_failure_threshold=2)

        result = service.repair_gaps(market="cn", governance_run_succeeded=False)

        refreshed_gap = db.list_kline_audit_gaps(market="cn", status="candidate_skip")[0]
        registry_row = db.get_kline_skip_registry(
            market="cn",
            gap_scope="symbol_range_gap",
            code="000001",
            missing_date_from=date(2026, 4, 10),
            missing_date_to=date(2026, 4, 11),
        )
        events = db.list_kline_audit_events(gap_key=gap.gap_key)

        assert result["candidate_skip_gap_count"] == 1
        assert refreshed_gap.gap_key == gap.gap_key
        assert registry_row is not None
        assert registry_row.status == "candidate_skip"
        assert registry_row.reason_type == "empty_data"
        assert [event.event_type for event in events] == [
            "repair_attempted",
            "repair_attempted",
            "promoted_to_candidate_skip",
        ]
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        temp_dir.cleanup()


def test_repair_service_recovers_candidate_skip_after_window_repaired():
    from src.services.kline_repair_service import KlineRepairService

    temp_dir = tempfile.TemporaryDirectory()
    try:
        db_path = os.path.join(temp_dir.name, "test_kline_repair_service.db")
        os.environ["DATABASE_PATH"] = db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()
        db = DatabaseManager.get_instance()
        _create_run(db, "repair-run-003")
        gap = db.upsert_kline_audit_gap(
            market="cn",
            gap_scope="symbol_range_gap",
            code="000001",
            missing_date_from=date(2026, 4, 10),
            missing_date_to=date(2026, 4, 11),
            source_run_id="repair-run-003",
            status="candidate_skip",
        )
        db.upsert_kline_skip_registry(
            market="cn",
            gap_scope="symbol_range_gap",
            code="000001",
            missing_date_from=date(2026, 4, 10),
            missing_date_to=date(2026, 4, 11),
            status="candidate_skip",
            reason_type="empty_data",
        )
        db.save_daily_data(
            _build_daily_rows([date(2026, 4, 10), date(2026, 4, 11)]),
            "000001",
            data_source="test",
        )

        service = KlineRepairService(db_manager=db, sync_service=_StubSyncService([]), candidate_failure_threshold=2)

        result = service.repair_gaps(market="cn", governance_run_succeeded=False)

        refreshed_gap = db.list_kline_audit_gaps(market="cn", status="healthy")[0]
        registry_row = db.get_kline_skip_registry(
            market="cn",
            gap_scope="symbol_range_gap",
            code="000001",
            missing_date_from=date(2026, 4, 10),
            missing_date_to=date(2026, 4, 11),
        )
        events = db.list_kline_audit_events(gap_key=gap.gap_key)

        assert result["recovered_gap_count"] == 1
        assert refreshed_gap.gap_key == gap.gap_key
        assert registry_row is not None
        assert registry_row.status == "healthy"
        assert registry_row.last_recovered_at is not None
        assert [event.event_type for event in events] == ["recovered"]
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        temp_dir.cleanup()


def test_repair_service_recovers_market_day_gap_without_counting_post_gap_new_listing():
    from src.services.kline_repair_service import KlineRepairService

    temp_dir = tempfile.TemporaryDirectory()
    try:
        db_path = os.path.join(temp_dir.name, "test_kline_repair_service.db")
        os.environ["DATABASE_PATH"] = db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()
        db = DatabaseManager.get_instance()
        _seed_active_instruments(db)
        _create_run(db, "repair-run-003b")
        gap = db.upsert_kline_audit_gap(
            market="cn",
            gap_scope="market_day_gap",
            trade_date=date(2026, 4, 16),
            source_run_id="repair-run-003b",
            status="candidate_skip",
        )
        db.save_daily_data(_build_daily_rows([date(2026, 4, 16)]), "000001", data_source="test")
        db.save_daily_data(_build_daily_rows([date(2026, 4, 16)]), "000002", data_source="test")
        db.upsert_instruments(
            [
                {
                    "code": "000003",
                    "name": "New Listing",
                    "market": "cn",
                    "exchange": "SZSE",
                    "listing_status": "active",
                    "is_st": False,
                    "industry": "Tech",
                    "list_date": date(2026, 4, 17),
                }
            ]
        )

        service = KlineRepairService(db_manager=db, sync_service=_StubSyncService([]), candidate_failure_threshold=2)

        result = service.repair_gaps(market="cn", governance_run_succeeded=False)

        healthy_gaps = db.list_kline_audit_gaps(market="cn", status="healthy")
        events = db.list_kline_audit_events(gap_key=gap.gap_key)

        assert result["recovered_gap_count"] == 1
        assert result["candidate_skip_gap_count"] == 0
        assert healthy_gaps[0].gap_key == gap.gap_key
        assert [event.event_type for event in events] == ["recovered"]
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        temp_dir.cleanup()


def test_repair_service_market_day_gap_recovery_ignores_st_instruments_for_cn_universe():
    from src.services.kline_repair_service import KlineRepairService

    temp_dir = tempfile.TemporaryDirectory()
    try:
        db_path = os.path.join(temp_dir.name, "test_kline_repair_service.db")
        os.environ["DATABASE_PATH"] = db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()
        db = DatabaseManager.get_instance()
        _seed_active_instruments(db)
        db.upsert_instruments(
            [
                {
                    "code": "000004",
                    "name": "*ST Test",
                    "market": "cn",
                    "exchange": "SZSE",
                    "listing_status": "active",
                    "is_st": True,
                    "industry": "Test",
                    "list_date": date(2000, 1, 1),
                }
            ]
        )
        _create_run(db, "repair-run-003c")
        db.upsert_kline_audit_gap(
            market="cn",
            gap_scope="market_day_gap",
            trade_date=date(2026, 4, 16),
            source_run_id="repair-run-003c",
            status="candidate_skip",
        )
        db.save_daily_data(_build_daily_rows([date(2026, 4, 16)]), "000001", data_source="test")
        db.save_daily_data(_build_daily_rows([date(2026, 4, 16)]), "000002", data_source="test")

        service = KlineRepairService(db_manager=db, sync_service=_StubSyncService([]), candidate_failure_threshold=2)

        result = service.repair_gaps(market="cn", governance_run_succeeded=False)

        candidate_gaps = db.list_kline_audit_gaps(market="cn", status="candidate_skip")
        healthy_gaps = db.list_kline_audit_gaps(market="cn", status="healthy")

        assert result["recovered_gap_count"] == 1
        assert candidate_gaps == []
        assert len(healthy_gaps) == 1
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        temp_dir.cleanup()


def test_repair_service_recovers_approved_skip_only_after_three_consecutive_successes():
    from src.services.kline_repair_service import KlineRepairService

    temp_dir = tempfile.TemporaryDirectory()
    try:
        db_path = os.path.join(temp_dir.name, "test_kline_repair_service.db")
        os.environ["DATABASE_PATH"] = db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()
        db = DatabaseManager.get_instance()
        _create_run(db, "repair-run-004")
        gap = db.upsert_kline_audit_gap(
            market="cn",
            gap_scope="symbol_range_gap",
            code="000001",
            missing_date_from=date(2026, 4, 10),
            missing_date_to=date(2026, 4, 11),
            source_run_id="repair-run-004",
            status="approved_skip",
        )
        db.upsert_kline_skip_registry(
            market="cn",
            gap_scope="symbol_range_gap",
            code="000001",
            missing_date_from=date(2026, 4, 10),
            missing_date_to=date(2026, 4, 11),
            status="approved_skip",
            approved_by="reviewer",
            reason_type="suspension",
        )
        db.save_daily_data(
            _build_daily_rows([date(2026, 4, 10), date(2026, 4, 11)]),
            "000001",
            data_source="test",
        )

        service = KlineRepairService(db_manager=db, sync_service=_StubSyncService([]), candidate_failure_threshold=2)

        first = service.repair_gaps(
            market="cn",
            governance_run_succeeded=True,
            governance_run_id="gov-run-1",
        )
        duplicate = service.repair_gaps(
            market="cn",
            governance_run_succeeded=True,
            governance_run_id="gov-run-1",
        )
        second = service.repair_gaps(
            market="cn",
            governance_run_succeeded=True,
            governance_run_id="gov-run-2",
        )
        third = service.repair_gaps(
            market="cn",
            governance_run_succeeded=True,
            governance_run_id="gov-run-3",
        )

        registry_row = db.get_kline_skip_registry(
            market="cn",
            gap_scope="symbol_range_gap",
            code="000001",
            missing_date_from=date(2026, 4, 10),
            missing_date_to=date(2026, 4, 11),
        )
        events = db.list_kline_audit_events(gap_key=gap.gap_key)
        healthy_gaps = db.list_kline_audit_gaps(market="cn", status="healthy")

        assert first["recovered_gap_count"] == 0
        assert duplicate["recovered_gap_count"] == 0
        assert second["recovered_gap_count"] == 0
        assert third["recovered_gap_count"] == 1
        assert registry_row is not None
        assert registry_row.status == "healthy"
        assert registry_row.success_streak == 3
        assert registry_row.last_recovered_at is not None
        assert healthy_gaps[0].gap_key == gap.gap_key
        assert [event.event_type for event in events] == [
            "success_streak_incremented",
            "success_streak_incremented",
            "recovered",
        ]
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        temp_dir.cleanup()


def test_repair_service_requires_governance_run_id_for_approved_skip_recovery():
    from src.services.kline_repair_service import KlineRepairService

    temp_dir = tempfile.TemporaryDirectory()
    try:
        db_path = os.path.join(temp_dir.name, "test_kline_repair_service.db")
        os.environ["DATABASE_PATH"] = db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()
        db = DatabaseManager.get_instance()
        _create_run(db, "repair-run-005")
        db.upsert_kline_audit_gap(
            market="cn",
            gap_scope="symbol_range_gap",
            code="000001",
            missing_date_from=date(2026, 4, 10),
            missing_date_to=date(2026, 4, 11),
            source_run_id="repair-run-005",
            status="approved_skip",
        )
        db.upsert_kline_skip_registry(
            market="cn",
            gap_scope="symbol_range_gap",
            code="000001",
            missing_date_from=date(2026, 4, 10),
            missing_date_to=date(2026, 4, 11),
            status="approved_skip",
            approved_by="reviewer",
            reason_type="suspension",
        )
        db.save_daily_data(
            _build_daily_rows([date(2026, 4, 10), date(2026, 4, 11)]),
            "000001",
            data_source="test",
        )

        service = KlineRepairService(db_manager=db, sync_service=_StubSyncService([]), candidate_failure_threshold=2)

        try:
            service.repair_gaps(market="cn", governance_run_succeeded=True)
            raise AssertionError("expected ValueError when governance_run_id is missing")
        except ValueError as exc:
            assert "governance_run_id" in str(exc)
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        temp_dir.cleanup()


def test_repair_service_skips_gaps_after_max_trade_date():
    from src.services.kline_repair_service import KlineRepairService

    temp_dir = tempfile.TemporaryDirectory()
    try:
        db_path = os.path.join(temp_dir.name, "test_kline_repair_service.db")
        os.environ["DATABASE_PATH"] = db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()
        db = DatabaseManager.get_instance()
        _create_run(db, "repair-run-cutoff")
        gap = db.upsert_kline_audit_gap(
            market="cn",
            gap_scope="symbol_range_gap",
            code="000001",
            missing_date_from=date(2026, 5, 12),
            missing_date_to=date(2026, 5, 12),
            source_run_id="repair-run-cutoff",
            status="pending_retry",
        )
        sync_service = _StubSyncService([{"errors": []}])
        service = KlineRepairService(db_manager=db, sync_service=sync_service)

        result = service.repair_gaps(
            market="cn",
            governance_run_succeeded=False,
            max_trade_date=date(2026, 5, 11),
        )

        pending_gaps = db.list_kline_audit_gaps(market="cn", status="pending_retry")
        assert result["skipped_gap_count"] == 1
        assert result["repaired_gap_count"] == 0
        assert sync_service.calls == []
        assert pending_gaps[0].gap_key == gap.gap_key
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        temp_dir.cleanup()


class _BatchRecordingSyncService:
    """记录每次 sync_trade_date 调用，并为请求的股票落库，模拟成功的批量拉取。"""

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db
        self.calls = []

    def sync_trade_date(self, trade_date, stock_codes=None, force=False, **kwargs):
        codes = list(stock_codes) if stock_codes else None
        self.calls.append({"trade_date": trade_date, "stock_codes": codes, "force": force})
        for code in codes or []:
            self.db.save_daily_data(_build_daily_rows([trade_date]), code, data_source="test")
        return {
            "trade_date": trade_date.isoformat(),
            "errors": [],
            "health_report": {"is_healthy": True},
        }


def test_repair_service_batches_symbol_gaps_by_trade_date():
    """多只股票缺同一批交易日时，应按交易日聚合：每个交易日只发一次同步，覆盖所有缺口股票。"""
    from src.services.kline_repair_service import KlineRepairService

    temp_dir = tempfile.TemporaryDirectory()
    try:
        db_path = os.path.join(temp_dir.name, "test_kline_repair_batch.db")
        os.environ["DATABASE_PATH"] = db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()
        db = DatabaseManager.get_instance()
        _create_run(db, "repair-run-batch")

        missing_from = date(2026, 4, 13)  # 周一
        missing_to = date(2026, 4, 15)    # 周三
        codes = ["000001", "000002", "000003"]
        for code in codes:
            db.upsert_kline_audit_gap(
                market="cn",
                gap_scope="symbol_range_gap",
                code=code,
                missing_date_from=missing_from,
                missing_date_to=missing_to,
                source_run_id="repair-run-batch",
                status="pending_retry",
            )

        sync_service = _BatchRecordingSyncService(db)
        service = KlineRepairService(db_manager=db, sync_service=sync_service)

        result = service.repair_gaps(market="cn", governance_run_succeeded=False)

        session_dates = KlineRepairService._iter_market_dates(
            market="cn", start=missing_from, end=missing_to
        )
        assert len(session_dates) >= 1

        # 核心断言：每个交易日只调用一次（按日聚合），而非每只股票各调一次
        assert len(sync_service.calls) == len(session_dates)
        for call in sync_service.calls:
            assert set(call["stock_codes"]) == set(codes)

        # 所有缺口都应被修复为 healthy
        assert result["repaired_gap_count"] == len(codes)
        healthy = db.list_kline_audit_gaps(market="cn", status="healthy")
        assert {g.code for g in healthy} == set(codes)
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        temp_dir.cleanup()
