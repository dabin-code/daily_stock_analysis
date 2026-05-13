import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.config import Config
from src.storage import DatabaseManager, StockDaily


@pytest.fixture()
def temp_db():
    temp_dir = tempfile.TemporaryDirectory()
    previous_database_path = os.environ.get("DATABASE_PATH")
    os.environ["DATABASE_PATH"] = os.path.join(temp_dir.name, "test.db")
    Config.reset_instance()
    DatabaseManager.reset_instance()
    db = DatabaseManager.get_instance()
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        if previous_database_path is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = previous_database_path
        temp_dir.cleanup()


def _insert_stock_day(db: DatabaseManager, code: str, trade_date: date) -> None:
    with db.session_scope() as session:
        session.add(
            StockDaily(
                code=code,
                date=trade_date,
                open=10.0,
                high=11.0,
                low=9.0,
                close=10.5,
                volume=1000.0,
                amount=10_500.0,
                pct_chg=1.0,
            )
        )


def test_list_available_trade_dates_reads_distinct_dates_in_range(temp_db):
    from scripts.run_historical_random_screening import list_available_trade_dates

    _insert_stock_day(temp_db, "600519", date(2024, 4, 18))
    _insert_stock_day(temp_db, "600519", date(2024, 4, 19))
    _insert_stock_day(temp_db, "000001", date(2024, 4, 19))
    _insert_stock_day(temp_db, "600519", date(2024, 4, 22))
    _insert_stock_day(temp_db, "600519", date(2024, 4, 23))

    dates = list_available_trade_dates(
        temp_db,
        start_date=date(2024, 4, 19),
        end_date=date(2024, 4, 22),
    )

    assert dates == [date(2024, 4, 19), date(2024, 4, 22)]


def test_sample_trade_dates_is_reproducible_and_sorted():
    from scripts.run_historical_random_screening import sample_trade_dates

    available = [date(2024, 4, day) for day in range(19, 29)]

    first = sample_trade_dates(available, sample_days=5, seed=42)
    second = sample_trade_dates(available, sample_days=5, seed=42)

    assert first == second
    assert first == sorted(first)
    assert len(first) == 5


def test_sample_trade_dates_raises_when_not_enough_dates():
    from scripts.run_historical_random_screening import sample_trade_dates

    with pytest.raises(ValueError, match="可用交易日不足"):
        sample_trade_dates([date(2024, 4, 19)], sample_days=2, seed=1)


def test_arg_parser_defaults_match_historical_sample_request():
    from scripts.run_historical_random_screening import build_arg_parser, options_from_args

    options = options_from_args(build_arg_parser().parse_args([]))

    assert options.start_date == date(2024, 4, 19)
    assert options.end_date == date(2026, 5, 11)
    assert options.sample_days == 100
    assert options.candidate_limit == 10
    assert options.ai_top_k == 0
    assert options.market == "cn"
    assert options.force is False


def test_arg_parser_accepts_force_and_seed():
    from scripts.run_historical_random_screening import build_arg_parser, options_from_args

    options = options_from_args(build_arg_parser().parse_args(["--force", "--seed", "123"]))

    assert options.force is True
    assert options.seed == 123


def test_script_entrypoint_can_run_by_file_path():
    repo_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [sys.executable, "scripts/run_historical_random_screening.py", "--help"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--sample-days" in result.stdout


def test_run_batch_dry_run_does_not_execute_screening(temp_db, capsys):
    from scripts.run_historical_random_screening import run_batch

    for day in range(19, 24):
        _insert_stock_day(temp_db, "600519", date(2024, 4, day))

    service = MagicMock()
    result = run_batch(
        db=temp_db,
        service=service,
        options=SimpleNamespace(
            start_date=date(2024, 4, 19),
            end_date=date(2024, 4, 23),
            sample_days=3,
            candidate_limit=10,
            ai_top_k=0,
            market="cn",
            seed=7,
            dry_run=True,
            force=False,
            continue_on_error=True,
        ),
    )

    assert result["selected_trade_dates"]
    assert result["success_count"] == 0
    service.execute_run.assert_not_called()
    assert "DRY RUN" in capsys.readouterr().out


def test_run_batch_executes_each_sampled_date_with_ai_disabled(temp_db):
    from scripts.run_historical_random_screening import run_batch

    for day in range(19, 24):
        _insert_stock_day(temp_db, "600519", date(2024, 4, day))

    service = MagicMock()
    service.execute_run.side_effect = [
        {"run_id": "run-1", "status": "completed", "candidate_count": 10},
        {"run_id": "run-2", "status": "completed", "candidate_count": 10},
    ]

    result = run_batch(
        db=temp_db,
        service=service,
        options=SimpleNamespace(
            start_date=date(2024, 4, 19),
            end_date=date(2024, 4, 23),
            sample_days=2,
            candidate_limit=10,
            ai_top_k=0,
            market="cn",
            seed=3,
            dry_run=False,
            force=False,
            continue_on_error=True,
        ),
    )

    assert result["success_count"] == 2
    assert result["failure_count"] == 0
    assert service.execute_run.call_count == 2
    for call in service.execute_run.call_args_list:
        assert call.kwargs["candidate_limit"] == 10
        assert call.kwargs["ai_top_k"] == 0
        assert call.kwargs["trigger_type"] == "historical_random_sample"


def test_run_batch_collects_failures_and_continues(temp_db):
    from scripts.run_historical_random_screening import run_batch

    for day in range(19, 22):
        _insert_stock_day(temp_db, "600519", date(2024, 4, day))

    service = MagicMock()
    service.execute_run.side_effect = [
        {"run_id": "run-1", "status": "completed", "candidate_count": 10},
        RuntimeError("boom"),
        {"run_id": "run-3", "status": "completed", "candidate_count": 10},
    ]

    result = run_batch(
        db=temp_db,
        service=service,
        options=SimpleNamespace(
            start_date=date(2024, 4, 19),
            end_date=date(2024, 4, 21),
            sample_days=3,
            candidate_limit=10,
            ai_top_k=0,
            market="cn",
            seed=1,
            dry_run=False,
            force=False,
            continue_on_error=True,
        ),
    )

    assert result["success_count"] == 2
    assert result["failure_count"] == 1
    assert result["failures"][0]["error"] == "boom"


def test_run_batch_does_not_reuse_non_historical_matching_run(temp_db):
    from scripts.run_historical_random_screening import run_batch

    trade_date = date(2024, 4, 19)
    _insert_stock_day(temp_db, "600519", trade_date)
    snapshot = {
        "requested_trade_date": "2024-04-19",
        "mode": "balanced",
        "stock_codes": [],
        "candidate_limit": 10,
        "ai_top_k": 0,
        "screening_min_list_days": 120,
        "screening_min_volume_ratio": 1.2,
        "screening_breakout_lookback_days": 20,
        "screening_factor_lookback_days": 80,
        "screening_ingest_failure_threshold": 0.2,
    }
    temp_db.create_screening_run(
        run_id="manual-run",
        trade_date=trade_date,
        market="cn",
        config_snapshot=snapshot,
        trigger_type="manual",
    )

    service = MagicMock()
    service.resolve_run_config.return_value = object()
    service._build_run_config_snapshot.return_value = snapshot

    result = run_batch(
        db=temp_db,
        service=service,
        options=SimpleNamespace(
            start_date=trade_date,
            end_date=trade_date,
            sample_days=1,
            candidate_limit=10,
            ai_top_k=0,
            market="cn",
            seed=1,
            dry_run=False,
            force=False,
            continue_on_error=True,
        ),
    )

    assert result["success_count"] == 0
    assert result["failure_count"] == 1
    assert "非历史抽样筛选记录" in result["failures"][0]["error"]
    service.execute_run.assert_not_called()


def test_force_delete_does_not_remove_non_historical_sample_run(temp_db):
    from scripts.run_historical_random_screening import (
        HistoricalRandomScreeningOptions,
        delete_existing_matching_run,
    )

    snapshot = {
        "requested_trade_date": "2024-04-19",
        "mode": "balanced",
        "stock_codes": [],
        "candidate_limit": 10,
        "ai_top_k": 0,
        "screening_min_list_days": 120,
        "screening_min_volume_ratio": 1.2,
        "screening_breakout_lookback_days": 20,
        "screening_factor_lookback_days": 80,
        "screening_ingest_failure_threshold": 0.2,
    }
    temp_db.create_screening_run(
        run_id="manual-run",
        trade_date=date(2024, 4, 19),
        market="cn",
        config_snapshot=snapshot,
        trigger_type="manual",
    )

    service = MagicMock()
    service.resolve_run_config.return_value = object()
    service._build_run_config_snapshot.return_value = snapshot

    deleted = delete_existing_matching_run(
        db=temp_db,
        service=service,
        trade_date=date(2024, 4, 19),
        options=HistoricalRandomScreeningOptions(force=True),
    )

    assert deleted is None
    service.delete_run.assert_not_called()
    assert temp_db.get_screening_run("manual-run") is not None


def test_force_delete_removes_historical_sample_run(temp_db):
    from scripts.run_historical_random_screening import (
        HistoricalRandomScreeningOptions,
        delete_existing_matching_run,
    )

    snapshot = {
        "requested_trade_date": "2024-04-19",
        "mode": "balanced",
        "stock_codes": [],
        "candidate_limit": 10,
        "ai_top_k": 0,
        "screening_min_list_days": 120,
        "screening_min_volume_ratio": 1.2,
        "screening_breakout_lookback_days": 20,
        "screening_factor_lookback_days": 80,
        "screening_ingest_failure_threshold": 0.2,
    }
    temp_db.create_screening_run(
        run_id="historical-run",
        trade_date=date(2024, 4, 19),
        market="cn",
        config_snapshot=snapshot,
        trigger_type="historical_random_sample",
    )

    service = MagicMock()
    service.resolve_run_config.return_value = object()
    service._build_run_config_snapshot.return_value = snapshot
    service.delete_run.side_effect = temp_db.delete_screening_run

    deleted = delete_existing_matching_run(
        db=temp_db,
        service=service,
        trade_date=date(2024, 4, 19),
        options=HistoricalRandomScreeningOptions(force=True),
    )

    assert deleted == "historical-run"
    assert temp_db.get_screening_run("historical-run") is None


def test_run_batch_force_does_not_fall_through_to_older_non_historical_run(temp_db):
    from scripts.run_historical_random_screening import run_batch

    trade_date = date(2024, 4, 19)
    _insert_stock_day(temp_db, "600519", trade_date)
    snapshot = {
        "requested_trade_date": "2024-04-19",
        "mode": "balanced",
        "stock_codes": [],
        "candidate_limit": 10,
        "ai_top_k": 0,
        "screening_min_list_days": 120,
        "screening_min_volume_ratio": 1.2,
        "screening_breakout_lookback_days": 20,
        "screening_factor_lookback_days": 80,
        "screening_ingest_failure_threshold": 0.2,
    }
    temp_db.create_screening_run(
        run_id="manual-run",
        trade_date=trade_date,
        market="cn",
        config_snapshot=snapshot,
        trigger_type="manual",
    )
    temp_db.create_screening_run(
        run_id="historical-run",
        trade_date=trade_date,
        market="cn",
        config_snapshot=snapshot,
        trigger_type="historical_random_sample",
    )

    service = MagicMock()
    service.resolve_run_config.return_value = object()
    service._build_run_config_snapshot.return_value = snapshot
    service.delete_run.side_effect = temp_db.delete_screening_run

    result = run_batch(
        db=temp_db,
        service=service,
        options=SimpleNamespace(
            start_date=trade_date,
            end_date=trade_date,
            sample_days=1,
            candidate_limit=10,
            ai_top_k=0,
            market="cn",
            seed=1,
            dry_run=False,
            force=True,
            continue_on_error=True,
        ),
    )

    assert result["success_count"] == 0
    assert result["failure_count"] == 1
    assert "非历史抽样筛选记录" in result["failures"][0]["error"]
    assert temp_db.get_screening_run("manual-run") is not None
    assert temp_db.get_screening_run("historical-run") is not None
    service.execute_run.assert_not_called()


def test_force_delete_helper_does_not_delete_when_any_non_historical_run_matches(temp_db):
    from scripts.run_historical_random_screening import (
        HistoricalRandomScreeningOptions,
        delete_existing_matching_run,
    )

    trade_date = date(2024, 4, 19)
    snapshot = {
        "requested_trade_date": "2024-04-19",
        "mode": "balanced",
        "stock_codes": [],
        "candidate_limit": 10,
        "ai_top_k": 0,
        "screening_min_list_days": 120,
        "screening_min_volume_ratio": 1.2,
        "screening_breakout_lookback_days": 20,
        "screening_factor_lookback_days": 80,
        "screening_ingest_failure_threshold": 0.2,
    }
    temp_db.create_screening_run(
        run_id="manual-run",
        trade_date=trade_date,
        market="cn",
        config_snapshot=snapshot,
        trigger_type="manual",
    )
    temp_db.create_screening_run(
        run_id="historical-run",
        trade_date=trade_date,
        market="cn",
        config_snapshot=snapshot,
        trigger_type="historical_random_sample",
    )

    service = MagicMock()
    service.resolve_run_config.return_value = object()
    service._build_run_config_snapshot.return_value = snapshot
    service.delete_run.side_effect = temp_db.delete_screening_run

    deleted = delete_existing_matching_run(
        db=temp_db,
        service=service,
        trade_date=trade_date,
        options=HistoricalRandomScreeningOptions(force=True),
    )

    assert deleted is None
    assert temp_db.get_screening_run("manual-run") is not None
    assert temp_db.get_screening_run("historical-run") is not None


def test_exit_code_is_non_zero_when_any_batch_item_failed():
    from scripts.run_historical_random_screening import exit_code_from_summary

    assert exit_code_from_summary({"failure_count": 1}) == 1
    assert exit_code_from_summary({"failure_count": 0}) == 0
