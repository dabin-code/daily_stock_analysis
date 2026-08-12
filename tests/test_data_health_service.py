from datetime import date

import pandas as pd
import pytest

from src.services.data_health_service import DataHealthService
from src.storage import DatabaseManager


@pytest.fixture
def db(tmp_path):
    """构造隔离的 DatabaseManager 并在用例结束后还原全局单例。

    DatabaseManager 是单例，直接构造会把临时库装成全局实例。
    不还原的话，后续所有 get_instance() 的调用者都会读到这个
    已被删除的临时库——这正是 test_e2e_five_layer_local 的板块热度
    用例在全量套件里失败、单独跑却通过的原因。
    """
    DatabaseManager.reset_instance()
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'data_health.db'}")
    try:
        yield manager
    finally:
        DatabaseManager.reset_instance()


def _seed_instruments(db: DatabaseManager) -> None:
    db.upsert_instruments(
        [
            {
                "code": "000001",
                "name": "平安银行",
                "market": "cn",
                "exchange": "SZSE",
                "listing_status": "active",
                "is_st": False,
                "industry": "银行",
                "list_date": date(1991, 4, 3),
            },
            {
                "code": "000002",
                "name": "万科A",
                "market": "cn",
                "exchange": "SZSE",
                "listing_status": "active",
                "is_st": False,
                "industry": "房地产",
                "list_date": date(1991, 1, 29),
            },
            {
                "code": "600000",
                "name": "浦发银行",
                "market": "cn",
                "exchange": "SSE",
                "listing_status": "active",
                "is_st": False,
                "industry": "银行",
                "list_date": date(1999, 11, 10),
            },
            {
                "code": "000003",
                "name": "退市样本",
                "market": "cn",
                "exchange": "SZSE",
                "listing_status": "delisted",
                "is_st": False,
                "industry": "样本",
                "list_date": date(1991, 1, 1),
            },
            {
                "code": "000004",
                "name": "ST样本",
                "market": "cn",
                "exchange": "SZSE",
                "listing_status": "active",
                "is_st": True,
                "industry": "样本",
                "list_date": date(1991, 1, 1),
            },
        ]
    )


def _save_daily(db: DatabaseManager, code: str, dates: list[date]) -> None:
    rows = [
        {
            "date": trade_date,
            "open": 10.0,
            "high": 10.5,
            "low": 9.5,
            "close": 10.2,
            "volume": 1000,
            "amount": 10_200,
            "pct_chg": 1.0,
        }
        for trade_date in dates
    ]
    db.save_daily_data(pd.DataFrame(rows), code, data_source="test")


def test_data_health_summary_reports_screening_readiness_and_gaps(db):
    _seed_instruments(db)
    _save_daily(db, "000001", [date(2025, 6, 3)])
    _save_daily(db, "000001", [date(2026, 5, 8), date(2026, 5, 11)])
    _save_daily(db, "000002", [date(2026, 5, 8), date(2026, 5, 11)])
    _save_daily(db, "600000", [date(2026, 5, 8)])
    run = db.create_kline_audit_run(
        run_id="audit-001",
        market="cn",
        trade_date=date(2026, 5, 11),
        run_type="daily",
        trigger_type="manual",
        run_result="degraded",
        pass_status="not_passed",
        rule_version="kline_audit_v1",
        window_start=date(2026, 5, 8),
        window_end=date(2026, 5, 11),
    )
    db.upsert_kline_audit_trade_date(
        market="cn",
        trade_date=date(2026, 5, 8),
        pass_status="passed",
        window_start=date(2026, 5, 8),
        window_end=date(2026, 5, 8),
        rule_version="kline_audit_v1",
        source_run_id=run.run_id,
        passed_at=None,
    )
    db.upsert_kline_audit_gap(
        market="cn",
        gap_scope="symbol_range_gap",
        code="600000",
        missing_date_from=date(2026, 5, 11),
        missing_date_to=date(2026, 5, 11),
        source_run_id=run.run_id,
        status="open",
    )

    summary = DataHealthService(db_manager=db, min_full_count=3).get_summary(market="cn")

    assert summary["expected_universe_count"] == 3
    assert summary["active_instrument_count"] == 4
    assert summary["st_excluded_count"] == 1
    assert summary["stock_data_start_date"] == "2025-06-03"
    assert summary["stock_data_end_date"] == "2026-05-11"
    assert summary["stock_data_trade_date_count"] == 3
    assert summary["latest_trade_date"] == "2026-05-11"
    assert summary["latest_complete_date"] == "2026-05-08"
    assert summary["latest_audit_passed_date"] == "2026-05-08"
    assert summary["latest_trade_date_synced_count"] == 2
    assert summary["latest_trade_date_coverage_ratio"] == 2 / 3
    assert summary["open_gap_count"] == 1
    assert summary["screening_ready"] is True
    assert summary["screening_ready_date"] == "2026-05-08"


def test_data_health_lists_coverage_and_gap_details(db):
    _seed_instruments(db)
    _save_daily(db, "000001", [date(2026, 5, 8), date(2026, 5, 11)])
    _save_daily(db, "000002", [date(2026, 5, 8)])
    run = db.create_kline_audit_run(
        run_id="audit-002",
        market="cn",
        trade_date=date(2026, 5, 11),
        run_type="daily",
        trigger_type="manual",
        run_result="degraded",
        pass_status="not_passed",
        rule_version="kline_audit_v1",
        window_start=date(2026, 5, 8),
        window_end=date(2026, 5, 11),
    )
    db.upsert_kline_audit_gap(
        market="cn",
        gap_scope="symbol_range_gap",
        code="000002",
        missing_date_from=date(2026, 5, 11),
        missing_date_to=date(2026, 5, 11),
        source_run_id=run.run_id,
        status="pending_retry",
    )

    service = DataHealthService(db_manager=db, min_full_count=3)

    coverage = service.get_coverage(market="cn", start=date(2026, 5, 8), end=date(2026, 5, 11))
    gaps = service.list_gaps(market="cn", status="pending_retry")

    assert coverage["items"] == [
        {
            "trade_date": "2026-05-08",
            "synced_count": 2,
            "expected_count": 3,
            "coverage_ratio": 2 / 3,
            "is_complete": False,
        },
        {
            "trade_date": "2026-05-11",
            "synced_count": 1,
            "expected_count": 3,
            "coverage_ratio": 1 / 3,
            "is_complete": False,
        },
    ]
    assert coverage["ma100_ready_count"] == 0
    assert coverage["ma200_ready_count"] == 0
    assert gaps["total"] == 1
    assert gaps["items"][0]["code"] == "000002"
    assert gaps["items"][0]["status"] == "pending_retry"
    assert gaps["items"][0]["missing_date_from"] == "2026-05-11"


def test_data_health_gap_list_defaults_to_unresolved_gaps(db):
    _seed_instruments(db)
    run = db.create_kline_audit_run(
        run_id="audit-003",
        market="cn",
        trade_date=date(2026, 5, 11),
        run_type="daily",
        trigger_type="manual",
        run_result="degraded",
        pass_status="not_passed",
        rule_version="kline_audit_v1",
        window_start=date(2026, 5, 8),
        window_end=date(2026, 5, 11),
    )
    for status, code in [
        ("healthy", "000001"),
        ("approved_skip", "000002"),
        ("open", "600000"),
        ("pending_retry", "000003"),
        ("candidate_skip", "000004"),
    ]:
        db.upsert_kline_audit_gap(
            market="cn",
            gap_scope="symbol_range_gap",
            code=code,
            missing_date_from=date(2026, 5, 11),
            missing_date_to=date(2026, 5, 11),
            source_run_id=run.run_id,
            status=status,
        )
    db.upsert_kline_audit_gap(
        market="cn",
        gap_scope="symbol_range_gap",
        code="000005",
        missing_date_from=date(2026, 5, 10),
        missing_date_to=date(2026, 5, 12),
        source_run_id=run.run_id,
        status="pending_retry",
    )

    service = DataHealthService(db_manager=db, min_full_count=3)

    default_gaps = service.list_gaps(market="cn")
    target_day_gaps = service.list_gaps(market="cn", end=date(2026, 5, 11))
    all_gaps = service.list_gaps(market="cn", status="all")
    healthy_gaps = service.list_gaps(market="cn", status="healthy")

    assert default_gaps["total"] == 4
    assert {item["status"] for item in default_gaps["items"]} == {
        "open",
        "pending_retry",
        "candidate_skip",
    }
    assert target_day_gaps["total"] == 3
    assert all(item["missing_date_to"] != "2026-05-12" for item in target_day_gaps["items"])
    assert all_gaps["total"] == 6
    assert healthy_gaps["total"] == 1
