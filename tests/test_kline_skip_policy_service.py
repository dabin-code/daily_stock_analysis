from datetime import date
from types import SimpleNamespace

from src.services.kline_skip_policy_service import KlineSkipPolicyService
from src.storage import DatabaseManager


def _build_db(tmp_path):
    DatabaseManager.reset_instance()
    return DatabaseManager(f"sqlite:///{tmp_path / 'kline_skip_policy.db'}")


def _build_config(**overrides):
    config = SimpleNamespace(
        kline_audit_auto_skip_enabled=True,
        kline_audit_auto_skip_max_symbols=20,
        kline_audit_auto_skip_max_ratio=0.005,
        kline_audit_auto_skip_min_coverage=0.99,
        kline_audit_auto_skip_reason_classes="skip_eligible",
        kline_audit_auto_skip_reasons="not_in_bulk_universe,empty_data",
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _create_run_and_gap(db, *, run_id="audit-1", code="300069", status="open"):
    db.create_kline_audit_run(
        run_id=run_id,
        market="cn",
        trade_date=date(2026, 5, 13),
        run_type="daily",
        trigger_type="scheduled",
        run_result="degraded",
        pass_status="not_passed",
        rule_version="kline_audit_v1",
        window_start=date(2026, 5, 1),
        window_end=date(2026, 5, 13),
    )
    return db.upsert_kline_audit_gap(
        market="cn",
        gap_scope="symbol_range_gap",
        code=code,
        missing_date_from=date(2026, 5, 13),
        missing_date_to=date(2026, 5, 13),
        source_run_id=run_id,
        status=status,
    )


def _create_market_day_gap(db, *, run_id="audit-1", status="open"):
    return db.upsert_kline_audit_gap(
        market="cn",
        gap_scope="market_day_gap",
        trade_date=date(2026, 5, 13),
        source_run_id=run_id,
        status=status,
    )


def test_auto_skip_policy_approves_small_skip_eligible_symbol_gap(tmp_path):
    db = _build_db(tmp_path)
    gap = _create_run_and_gap(db)
    _create_market_day_gap(db)
    service = KlineSkipPolicyService(config=_build_config(), db_manager=db)

    result = service.apply_auto_skip(
        market="cn",
        source_run_id="audit-1",
        trade_date=date(2026, 5, 13),
        sync_result={
            "total": 5309,
            "errors": [
                {
                    "code": "300069",
                    "target_trade_date": "2026-05-13",
                    "reason": "not_in_bulk_universe",
                    "reason_class": "skip_eligible",
                    "source_attempts": [{"source": "tushare_bulk_sentinel"}],
                }
            ],
            "health_report": {
                "expected_count": 5309,
                "available_count": 5308,
                "missing_count": 1,
                "reason_class_counts": {"blocking": 0, "retryable": 0, "skip_eligible": 1},
            },
        },
    )

    assert result["approved_count"] == 1
    registry_row = db.get_kline_skip_registry(
        market="cn",
        gap_scope="symbol_range_gap",
        code="300069",
        missing_date_from=date(2026, 5, 13),
        missing_date_to=date(2026, 5, 13),
    )
    assert registry_row is not None
    assert registry_row.status == "approved_skip"
    assert registry_row.reason_type == "auto_small_skip_eligible_gap"
    updated_gap = db.list_kline_audit_gaps(market="cn", status="approved_skip")[0]
    assert updated_gap.gap_key == gap.gap_key
    healthy_market_gap = db.list_kline_audit_gaps(market="cn", status="healthy")[0]
    assert healthy_market_gap.gap_scope == "market_day_gap"


def test_auto_skip_policy_rejects_blocking_or_large_gap_batches(tmp_path):
    db = _build_db(tmp_path)
    _create_run_and_gap(db)
    service = KlineSkipPolicyService(
        config=_build_config(kline_audit_auto_skip_max_symbols=1),
        db_manager=db,
    )

    result = service.apply_auto_skip(
        market="cn",
        source_run_id="audit-1",
        trade_date=date(2026, 5, 13),
        sync_result={
            "total": 100,
            "errors": [
                {
                    "code": "300069",
                    "target_trade_date": "2026-05-13",
                    "reason": "not_in_bulk_universe",
                    "reason_class": "skip_eligible",
                },
                {
                    "code": "000001",
                    "target_trade_date": "2026-05-13",
                    "reason": "save_failed",
                    "reason_class": "blocking",
                },
            ],
        },
    )

    assert result["approved_count"] == 0
    assert result["skipped_reason"] == "non_eligible_errors_present"
    assert db.list_kline_skip_registry(market="cn") == []


def test_auto_skip_policy_can_approve_current_gap_after_repair_promotes_status(tmp_path):
    db = _build_db(tmp_path)
    _create_run_and_gap(db, status="candidate_skip")
    service = KlineSkipPolicyService(config=_build_config(), db_manager=db)

    result = service.apply_auto_skip(
        market="cn",
        source_run_id="audit-1",
        trade_date=date(2026, 5, 13),
        sync_result={
            "total": 5309,
            "errors": [
                {
                    "code": "300069",
                    "target_trade_date": "2026-05-13",
                    "reason": "not_in_bulk_universe",
                    "reason_class": "skip_eligible",
                }
            ],
        },
    )

    assert result["approved_count"] == 1
    assert db.list_kline_audit_gaps(market="cn", status="approved_skip")[0].code == "300069"


def test_auto_skip_policy_closes_market_gap_when_other_symbols_are_already_healthy(tmp_path):
    db = _build_db(tmp_path)
    _create_run_and_gap(db, code="300069", status="candidate_skip")
    _create_run_and_gap(db, code="000001", status="healthy")
    _create_market_day_gap(db, status="pending_retry")
    service = KlineSkipPolicyService(config=_build_config(), db_manager=db)

    result = service.apply_auto_skip(
        market="cn",
        source_run_id="audit-1",
        trade_date=date(2026, 5, 13),
        sync_result={
            "total": 5309,
            "errors": [
                {
                    "code": "300069",
                    "target_trade_date": "2026-05-13",
                    "reason": "not_in_bulk_universe",
                    "reason_class": "skip_eligible",
                }
            ],
        },
    )

    assert result["approved_count"] == 1
    market_gap = [
        gap
        for gap in db.list_kline_audit_gaps(market="cn", gap_scope="market_day_gap")
        if gap.trade_date == date(2026, 5, 13)
    ][0]
    assert market_gap.status == "healthy"


def test_auto_skip_policy_approves_multi_day_gap_still_open_today(tmp_path):
    """停牌跨多日、且截至当日仍未结束(to == trade_date)的缺口应被自动豁免。

    审计窗口 window_end == trade_date,故缺口 missing_date_to 不会超过当日;
    当某代码当日仍 skip_eligible(not_in_bulk_universe)且其连续缺口正好截止当日时,
    该连续停牌区间属于「小而可解释」的缺口,应整段豁免——这是对原单日策略的有意放宽。
    """
    db = _build_db(tmp_path)
    db.create_kline_audit_run(
        run_id="audit-1",
        market="cn",
        trade_date=date(2026, 5, 13),
        run_type="daily",
        trigger_type="scheduled",
        run_result="degraded",
        pass_status="not_passed",
        rule_version="kline_audit_v1",
        window_start=date(2026, 5, 1),
        window_end=date(2026, 5, 13),
    )
    multi_day_gap = db.upsert_kline_audit_gap(
        market="cn",
        gap_scope="symbol_range_gap",
        code="300069",
        missing_date_from=date(2026, 5, 12),
        missing_date_to=date(2026, 5, 13),
        source_run_id="audit-1",
        status="open",
    )
    service = KlineSkipPolicyService(config=_build_config(), db_manager=db)

    result = service.apply_auto_skip(
        market="cn",
        source_run_id="audit-1",
        trade_date=date(2026, 5, 13),
        sync_result={
            "total": 5309,
            "errors": [
                {
                    "code": "300069",
                    "target_trade_date": "2026-05-13",
                    "reason": "not_in_bulk_universe",
                    "reason_class": "skip_eligible",
                    "source_attempts": [{"source": "tushare_bulk_sentinel"}],
                }
            ],
            "health_report": {
                "expected_count": 5309,
                "available_count": 5308,
                "missing_count": 1,
                "reason_class_counts": {"blocking": 0, "retryable": 0, "skip_eligible": 1},
            },
        },
    )

    assert result["approved_count"] == 1
    registry_row = db.get_kline_skip_registry(
        market="cn",
        gap_scope="symbol_range_gap",
        code="300069",
        missing_date_from=date(2026, 5, 12),
        missing_date_to=date(2026, 5, 13),
    )
    assert registry_row is not None
    assert registry_row.status == "approved_skip"
    approved_gap = db.list_kline_audit_gaps(market="cn", status="approved_skip")[0]
    assert approved_gap.gap_key == multi_day_gap.gap_key


def test_auto_skip_policy_does_not_approve_multi_day_gap_not_ending_today(tmp_path):
    """历史已结束的停牌缺口(to < trade_date)缺乏当日证据,不应被当日 auto-skip 豁免。"""
    db = _build_db(tmp_path)
    _create_run_and_gap(db)  # 当日单日缺口 [05-13, 05-13]
    historical_gap = db.upsert_kline_audit_gap(
        market="cn",
        gap_scope="symbol_range_gap",
        code="300069",
        missing_date_from=date(2026, 5, 9),
        missing_date_to=date(2026, 5, 10),
        source_run_id="audit-1",
        status="open",
    )
    service = KlineSkipPolicyService(config=_build_config(), db_manager=db)

    result = service.apply_auto_skip(
        market="cn",
        source_run_id="audit-1",
        trade_date=date(2026, 5, 13),
        sync_result={
            "total": 5309,
            "errors": [
                {
                    "code": "300069",
                    "target_trade_date": "2026-05-13",
                    "reason": "not_in_bulk_universe",
                    "reason_class": "skip_eligible",
                }
            ],
        },
    )

    # 仅当日缺口被豁免;历史缺口(to=05-10)保持 open
    assert result["approved_count"] == 1
    untouched = [
        gap
        for gap in db.list_kline_audit_gaps(market="cn")
        if gap.missing_date_from == date(2026, 5, 9)
    ][0]
    assert untouched.gap_key == historical_gap.gap_key
    assert untouched.status == "open"
