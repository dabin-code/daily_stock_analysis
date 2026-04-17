import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

from src.config import Config
from src.storage import DatabaseManager


def test_approve_skip_script_marks_candidate_as_approved_skip():
    temp_dir = tempfile.TemporaryDirectory()
    try:
        db_path = os.path.join(temp_dir.name, "test_approve_kline_skip.db")
        env = os.environ.copy()
        env["DATABASE_PATH"] = db_path

        os.environ["DATABASE_PATH"] = db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()
        db = DatabaseManager.get_instance()
        db.create_kline_audit_run(
            run_id="approve-run-001",
            market="cn",
            trade_date=date(2026, 4, 11),
            run_type="daily",
            trigger_type="manual",
            run_result="degraded",
            pass_status="not_passed",
            rule_version="kline_audit_v1",
            window_start=date(2026, 4, 10),
            window_end=date(2026, 4, 11),
        )
        gap = db.upsert_kline_audit_gap(
            market="cn",
            gap_scope="symbol_range_gap",
            code="000001",
            missing_date_from=date(2026, 4, 10),
            missing_date_to=date(2026, 4, 11),
            source_run_id="approve-run-001",
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
            notes="awaiting approval",
        )

        script_path = Path(__file__).resolve().parents[1] / "scripts" / "approve_kline_skip.py"
        completed = subprocess.run(
            [
                sys.executable,
                str(script_path),
                "--market",
                "cn",
                "--code",
                "000001",
                "--from-date",
                "2026-04-10",
                "--to-date",
                "2026-04-11",
                "--approved-by",
                "ops-user",
                "--reason-type",
                "manual_review",
                "--notes",
                "approved after verification",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode == 0, completed.stderr

        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ["DATABASE_PATH"] = db_path
        db = DatabaseManager.get_instance()
        row = db.get_kline_skip_registry(
            market="cn",
            gap_scope="symbol_range_gap",
            code="000001",
            missing_date_from=date(2026, 4, 10),
            missing_date_to=date(2026, 4, 11),
        )

        assert row is not None
        assert row.status == "approved_skip"
        assert row.approved_by == "ops-user"
        assert row.approved_at is not None
        assert row.reason_type == "manual_review"
        assert row.notes == "approved after verification"
        updated_gap = db.list_kline_audit_gaps(market="cn", status="approved_skip")[0]
        events = db.list_kline_audit_events(gap_key=gap.gap_key)
        assert updated_gap.gap_key == gap.gap_key
        assert [event.event_type for event in events] == ["approved_skip_granted"]
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        temp_dir.cleanup()


def test_approve_skip_script_preserves_existing_notes_when_notes_omitted():
    temp_dir = tempfile.TemporaryDirectory()
    try:
        db_path = os.path.join(temp_dir.name, "test_approve_kline_skip.db")
        env = os.environ.copy()
        env["DATABASE_PATH"] = db_path

        os.environ["DATABASE_PATH"] = db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()
        db = DatabaseManager.get_instance()
        db.create_kline_audit_run(
            run_id="approve-run-002",
            market="cn",
            trade_date=date(2026, 4, 11),
            run_type="daily",
            trigger_type="manual",
            run_result="degraded",
            pass_status="not_passed",
            rule_version="kline_audit_v1",
            window_start=date(2026, 4, 10),
            window_end=date(2026, 4, 11),
        )
        db.upsert_kline_audit_gap(
            market="cn",
            gap_scope="symbol_range_gap",
            code="000001",
            missing_date_from=date(2026, 4, 10),
            missing_date_to=date(2026, 4, 11),
            source_run_id="approve-run-002",
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
            notes="keep existing notes",
        )

        script_path = Path(__file__).resolve().parents[1] / "scripts" / "approve_kline_skip.py"
        completed = subprocess.run(
            [
                sys.executable,
                str(script_path),
                "--market",
                "cn",
                "--code",
                "000001",
                "--from-date",
                "2026-04-10",
                "--to-date",
                "2026-04-11",
                "--approved-by",
                "ops-user",
                "--reason-type",
                "manual_review",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode == 0, completed.stderr

        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ["DATABASE_PATH"] = db_path
        db = DatabaseManager.get_instance()
        row = db.get_kline_skip_registry(
            market="cn",
            gap_scope="symbol_range_gap",
            code="000001",
            missing_date_from=date(2026, 4, 10),
            missing_date_to=date(2026, 4, 11),
        )

        assert row is not None
        assert row.status == "approved_skip"
        assert row.notes == "keep existing notes"
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        temp_dir.cleanup()


def test_approve_skip_script_marks_market_day_candidate_as_approved_skip():
    temp_dir = tempfile.TemporaryDirectory()
    try:
        db_path = os.path.join(temp_dir.name, "test_approve_kline_skip_market_day.db")
        env = os.environ.copy()
        env["DATABASE_PATH"] = db_path

        os.environ["DATABASE_PATH"] = db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()
        db = DatabaseManager.get_instance()
        db.create_kline_audit_run(
            run_id="approve-run-003",
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
        gap = db.upsert_kline_audit_gap(
            market="cn",
            gap_scope="market_day_gap",
            trade_date=date(2026, 4, 16),
            source_run_id="approve-run-003",
            status="candidate_skip",
        )
        db.upsert_kline_skip_registry(
            market="cn",
            gap_scope="market_day_gap",
            trade_date=date(2026, 4, 16),
            status="candidate_skip",
            reason_type="source_unavailable",
            notes="awaiting market-day approval",
        )

        script_path = Path(__file__).resolve().parents[1] / "scripts" / "approve_kline_skip.py"
        completed = subprocess.run(
            [
                sys.executable,
                str(script_path),
                "--market",
                "cn",
                "--trade-date",
                "2026-04-16",
                "--approved-by",
                "ops-user",
                "--reason-type",
                "manual_review",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode == 0, completed.stderr

        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ["DATABASE_PATH"] = db_path
        db = DatabaseManager.get_instance()
        row = db.get_kline_skip_registry(
            market="cn",
            gap_scope="market_day_gap",
            trade_date=date(2026, 4, 16),
        )

        assert row is not None
        assert row.status == "approved_skip"
        assert row.approved_by == "ops-user"
        assert row.reason_type == "manual_review"
        updated_gap = db.list_kline_audit_gaps(market="cn", status="approved_skip")[0]
        events = db.list_kline_audit_events(gap_key=gap.gap_key)
        assert updated_gap.gap_key == gap.gap_key
        assert [event.event_type for event in events] == ["approved_skip_granted"]
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        temp_dir.cleanup()
