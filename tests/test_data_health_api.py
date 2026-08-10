from __future__ import annotations

import os
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

import src.auth as auth
from api.app import create_app
from src.config import Config
from src.services import data_health_task_service as data_health_tasks
from src.storage import DatabaseManager


def _reset_auth_globals() -> None:
    auth._auth_enabled = None
    auth._session_secret = None
    auth._password_hash_salt = None
    auth._password_hash_stored = None
    auth._rate_limit = {}


def _reset_data_health_task_globals() -> None:
    service = data_health_tasks._DEFAULT_TASK_SERVICE
    data_health_tasks._DEFAULT_TASK_SERVICE = None
    if service is not None:
        service._executor.shutdown(wait=True, cancel_futures=True)


def _build_client(tmp_dir: tempfile.TemporaryDirectory) -> tuple[TestClient, DatabaseManager]:
    _reset_auth_globals()
    _reset_data_health_task_globals()
    data_dir = Path(tmp_dir.name)
    db_path = data_dir / "data_health_api.db"
    env_path = data_dir / ".env"
    env_path.write_text(
        "\n".join(
            [
                "STOCK_LIST=000001",
                "ADMIN_AUTH_ENABLED=false",
                f"DATABASE_PATH={db_path}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    os.environ["ENV_FILE"] = str(env_path)
    os.environ["DATABASE_PATH"] = str(db_path)
    Config.reset_instance()
    DatabaseManager.reset_instance()
    app = create_app(static_dir=data_dir / "empty-static")
    return TestClient(app), DatabaseManager.get_instance()


def _seed_data(db: DatabaseManager) -> None:
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
            }
        ]
    )
    db.save_daily_data(
        pd.DataFrame(
            [
                {
                    "date": date(2026, 5, 8),
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.5,
                    "close": 10.2,
                    "volume": 1000,
                    "amount": 10_200,
                    "pct_chg": 1.0,
                }
            ]
        ),
        "000001",
        data_source="api-test",
    )


def _seed_gaps(db: DatabaseManager) -> None:
    run = db.create_kline_audit_run(
        run_id="api-audit-001",
        market="cn",
        trade_date=date(2026, 5, 8),
        run_type="daily",
        trigger_type="manual",
        run_result="degraded",
        pass_status="not_passed",
        rule_version="kline_audit_v1",
        window_start=date(2026, 5, 8),
        window_end=date(2026, 5, 8),
    )
    db.upsert_kline_audit_gap(
        market="cn",
        gap_scope="symbol_range_gap",
        code="000001",
        missing_date_from=date(2026, 5, 8),
        missing_date_to=date(2026, 5, 8),
        source_run_id=run.run_id,
        status="healthy",
    )
    db.upsert_kline_audit_gap(
        market="cn",
        gap_scope="symbol_range_gap",
        code="000002",
        missing_date_from=date(2026, 5, 8),
        missing_date_to=date(2026, 5, 8),
        source_run_id=run.run_id,
        status="open",
    )
    db.upsert_kline_audit_gap(
        market="cn",
        gap_scope="symbol_range_gap",
        code="000003",
        missing_date_from=date(2026, 5, 12),
        missing_date_to=date(2026, 5, 12),
        source_run_id=run.run_id,
        status="pending_retry",
    )


def test_data_health_api_exposes_summary_coverage_and_gaps():
    tmp_dir = tempfile.TemporaryDirectory()
    try:
        client, db = _build_client(tmp_dir)
        _seed_data(db)
        _seed_gaps(db)

        summary = client.get("/api/v1/data-health/summary")
        coverage = client.get("/api/v1/data-health/coverage", params={"from": "2026-05-08", "to": "2026-05-08"})
        gaps = client.get("/api/v1/data-health/gaps", params={"to": "2026-05-08"})
        all_gaps = client.get("/api/v1/data-health/gaps", params={"status": "all"})

        assert summary.status_code == 200
        assert summary.json()["expected_universe_count"] == 1
        assert summary.json()["latest_trade_date"] == "2026-05-08"
        assert coverage.status_code == 200
        assert coverage.json()["items"][0]["trade_date"] == "2026-05-08"
        assert gaps.status_code == 200
        assert gaps.json()["total"] == 1
        assert gaps.json()["items"][0]["status"] == "open"
        assert all_gaps.status_code == 200
        assert all_gaps.json()["total"] == 3
    finally:
        _reset_data_health_task_globals()
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("ENV_FILE", None)
        os.environ.pop("DATABASE_PATH", None)
        tmp_dir.cleanup()


def test_data_health_api_submits_operation_and_returns_task_status():
    tmp_dir = tempfile.TemporaryDirectory()
    try:
        client, _db = _build_client(tmp_dir)

        created = client.post(
            "/api/v1/data-health/operations",
            json={"operation_type": "repair_gaps", "market": "cn"},
        )

        assert created.status_code == 200
        task_id = created.json()["task_id"]
        fetched = client.get(f"/api/v1/data-health/tasks/{task_id}")
        assert fetched.status_code == 200
        assert fetched.json()["task_id"] == task_id
        assert fetched.json()["operation_type"] == "repair_gaps"
    finally:
        _reset_data_health_task_globals()
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("ENV_FILE", None)
        os.environ.pop("DATABASE_PATH", None)
        tmp_dir.cleanup()
