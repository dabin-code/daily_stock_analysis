from datetime import date, datetime

import pytest
from sqlalchemy import inspect

from src.storage import (
    DatabaseManager,
    KlineAuditEvent,
    KlineAuditGap,
    KlineAuditRun,
    KlineAuditTradeDate,
    KlineSkipRegistry,
)


def _build_db(tmp_path):
    DatabaseManager.reset_instance()
    db = DatabaseManager(f"sqlite:///{tmp_path / 'kline_audit_storage.db'}")
    return db


def test_kline_audit_tables_store_gap_and_trade_date_status(tmp_path):
    db = _build_db(tmp_path)

    run = db.create_kline_audit_run(
        run_id="audit-run-001",
        market="cn",
        trade_date=date(2026, 4, 16),
        run_type="daily",
        trigger_type="manual",
        run_result="degraded",
        pass_status="failed",
        rule_version="2026-04-16",
        window_start=date(2026, 4, 1),
        window_end=date(2026, 4, 16),
    )
    trade_date_status = db.upsert_kline_audit_trade_date(
        market="cn",
        trade_date=date(2026, 4, 16),
        pass_status="failed",
        window_start=date(2026, 4, 1),
        window_end=date(2026, 4, 16),
        rule_version="2026-04-16",
        source_run_id=run.run_id,
        passed_at=None,
    )
    gap = db.upsert_kline_audit_gap(
        market="cn",
        gap_scope="market_day_gap",
        source_run_id=run.run_id,
        trade_date=date(2026, 4, 16),
        status="open",
    )
    event = db.append_kline_audit_event(
        source_run_id=run.run_id,
        gap_key=gap.gap_key,
        event_type="gap_detected",
        event_status="open",
        payload={"trade_date": "2026-04-16"},
    )

    with db.session_scope() as session:
        stored_run = session.query(KlineAuditRun).filter_by(run_id="audit-run-001").one()
        stored_trade_date = (
            session.query(KlineAuditTradeDate)
            .filter_by(market="cn", trade_date=date(2026, 4, 16))
            .one()
        )
        stored_gap = session.query(KlineAuditGap).filter_by(gap_key=gap.gap_key).one()
        stored_event = session.query(KlineAuditEvent).filter_by(id=event.id).one()
        assert stored_run.run_result == "degraded"
        assert trade_date_status.id == stored_trade_date.id
        assert stored_trade_date.pass_status == "failed"
        assert stored_trade_date.source_run_id == run.run_id
        assert stored_gap.gap_key == "cn|2026-04-16|market_day_gap"
        assert stored_event.gap_key == stored_gap.gap_key
        assert stored_event.source_run_id == run.run_id


def test_skip_registry_supports_symbol_and_date_range_scope(tmp_path):
    db = _build_db(tmp_path)

    symbol_scope = db.upsert_kline_skip_registry(
        market="cn",
        gap_scope="symbol_range_gap",
        code="000001",
        missing_date_from=date(2026, 4, 10),
        missing_date_to=date(2026, 4, 12),
        status="approved_skip",
        approved_by="tester",
        approved_at=datetime(2026, 4, 17, 9, 30, 0),
        reason_type="suspension",
        notes="manual approval",
        success_streak=2,
    )
    market_scope = db.upsert_kline_skip_registry(
        market="cn",
        gap_scope="market_day_gap",
        trade_date=date(2026, 4, 15),
        status="candidate_skip",
        reason_type="calendar_mismatch",
        success_streak=0,
    )

    rows = db.list_kline_skip_registry(market="cn")

    assert len(rows) == 2
    assert symbol_scope.code == "000001"
    assert symbol_scope.missing_date_from == date(2026, 4, 10)
    assert symbol_scope.missing_date_to == date(2026, 4, 12)
    assert symbol_scope.status == "approved_skip"
    assert market_scope.trade_date == date(2026, 4, 15)
    assert {row.gap_scope for row in rows} == {"symbol_range_gap", "market_day_gap"}


def test_kline_audit_trade_date_contract_fields_present(tmp_path):
    db = _build_db(tmp_path)

    columns = {
        column["name"]: column
        for column in inspect(db._engine).get_columns("kline_audit_trade_dates")
    }

    for field_name in ("window_start", "window_end", "rule_version", "source_run_id", "passed_at"):
        assert field_name in columns


def test_skip_registry_upsert_preserves_existing_review_metadata(tmp_path):
    db = _build_db(tmp_path)

    created = db.upsert_kline_skip_registry(
        market="cn",
        gap_scope="symbol_range_gap",
        code="000001",
        missing_date_from=date(2026, 4, 10),
        missing_date_to=date(2026, 4, 12),
        status="approved_skip",
        approved_by="reviewer",
        approved_at=datetime(2026, 4, 17, 9, 30, 0),
        reason_type="manual_review",
        notes="keep this note",
        success_streak=1,
        last_recovered_at=datetime(2026, 4, 17, 10, 0, 0),
    )

    updated = db.upsert_kline_skip_registry(
        market="cn",
        gap_scope="symbol_range_gap",
        code="000001",
        missing_date_from=date(2026, 4, 10),
        missing_date_to=date(2026, 4, 12),
        status="healthy",
        success_streak=3,
        last_success_at=datetime(2026, 4, 18, 10, 0, 0),
    )

    assert updated.id == created.id
    assert updated.status == "healthy"
    assert updated.success_streak == 3
    assert updated.approved_by == "reviewer"
    assert updated.reason_type == "manual_review"
    assert updated.notes == "keep this note"
    assert updated.last_recovered_at == datetime(2026, 4, 17, 10, 0, 0)


def test_skip_registry_rejects_invalid_scope_payload(tmp_path):
    db = _build_db(tmp_path)

    with pytest.raises(ValueError, match="trade_date"):
        db.upsert_kline_skip_registry(
            market="cn",
            gap_scope="market_day_gap",
            status="candidate_skip",
        )


def test_skip_registry_schema_exposes_unique_skip_key_contract(tmp_path):
    db = _build_db(tmp_path)

    columns = {
        column["name"]: column
        for column in inspect(db._engine).get_columns("kline_skip_registry")
    }
    unique_constraints = inspect(db._engine).get_unique_constraints("kline_skip_registry")
    unique_indexes = [
        index
        for index in inspect(db._engine).get_indexes("kline_skip_registry")
        if index.get("unique")
    ]

    assert "skip_key" in columns
    assert any(
        constraint["column_names"] == ["skip_key"]
        for constraint in unique_constraints
    ) or any(
        index["column_names"] == ["skip_key"]
        for index in unique_indexes
    )


def test_skip_registry_duplicate_upsert_keeps_single_row_per_scope(tmp_path):
    db = _build_db(tmp_path)

    first = db.upsert_kline_skip_registry(
        market="cn",
        gap_scope="market_day_gap",
        trade_date=date(2026, 4, 15),
        status="candidate_skip",
        reason_type="calendar_mismatch",
        success_streak=0,
    )
    second = db.upsert_kline_skip_registry(
        market="cn",
        gap_scope="market_day_gap",
        trade_date=date(2026, 4, 15),
        status="approved_skip",
        approved_by="reviewer",
        approved_at=datetime(2026, 4, 17, 10, 0, 0),
        success_streak=1,
    )

    with db.session_scope() as session:
        rows = session.query(KlineSkipRegistry).all()
        assert len(rows) == 1
        assert rows[0].status == "approved_skip"
        assert rows[0].skip_key == "cn|2026-04-15|market_day_gap"

    assert first.id == second.id
