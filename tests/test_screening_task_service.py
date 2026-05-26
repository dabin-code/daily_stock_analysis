import os
import tempfile
import logging
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.config import Config
from src.services.candidate_analysis_service import CandidateAnalysisBatchResult
from src.services.screening_task_service import ScreeningTaskService, ScreeningTradeDateNotReadyError
from src.services.theme_context_ingest_service import ExternalTheme, OpenClawThemeContext
from src.services.universe_service import LocalUniverseNotReadyError
from src.storage import DatabaseManager


class _IntegrationUniverseService:
    def resolve_universe(self, stock_codes=None):
        raise LocalUniverseNotReadyError("本地 instrument_master 为空")

    def sync_universe(self, market="cn"):
        raise RuntimeError("sync failed")


class _SuccessUniverseService:
    def resolve_universe(self, stock_codes=None):
        return pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])


@pytest.fixture(autouse=True)
def _block_real_screening_notifications(monkeypatch):
    """Unit tests should never send real screening notifications."""
    notify_calls = []

    def _fake_notify_run(self, run_id, force=False):
        notify_calls.append({"run_id": run_id, "force": force})
        return {
            "success": True,
            "notification_status": "sent",
            "run_id": run_id,
            "candidate_count": 0,
        }

    monkeypatch.setattr(
        "src.services.screening_notification_service.ScreeningNotificationService.notify_run",
        _fake_notify_run,
    )
    return notify_calls


def test_screening_task_service_executes_full_pipeline_and_limits_ai_top_k():
    db = MagicMock()
    db.create_screening_run.return_value = "run-001"
    db.get_screening_run.return_value = {
        "run_id": "run-001",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 2,
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [
            {"code": "600519", "name": "贵州茅台"},
            {"code": "000001", "name": "平安银行"},
        ]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_service.build_factor_snapshot.return_value = pd.DataFrame(
        [
            {
                "code": "600519",
                "name": "贵州茅台",
                "close": 1500.0,
                "ma5": 1492.0,
                "ma10": 1485.0,
                "ma20": 1470.0,
                "ma60": 1420.0,
                "volume_ratio": 1.5,
                "avg_amount": 150_000_000,
                "breakout_ratio": 1.01,
                "pct_chg": 2.5,
                "is_st": False,
                "days_since_listed": 8000,
            },
            {
                "code": "000001",
                "name": "平安银行",
                "close": 12.0,
                "ma5": 11.9,
                "ma10": 11.8,
                "ma20": 11.7,
                "ma60": 11.5,
                "volume_ratio": 1.2,
                "avg_amount": 80_000_000,
                "breakout_ratio": 0.99,
                "pct_chg": 1.0,
                "is_st": False,
                "days_since_listed": 5000,
            },
        ]
    )

    screener_service = MagicMock()
    screener_service.evaluate.return_value.selected = [
        {
            "code": "600519",
            "name": "贵州茅台",
            "rank": 1,
            "rule_score": 91.0,
            "rule_hits": ["trend_aligned", "volume_expanding"],
            "factor_snapshot": {"close": 1500.0},
        },
        {
            "code": "000001",
            "name": "平安银行",
            "rank": 2,
            "rule_score": 80.0,
            "rule_hits": ["trend_aligned"],
            "factor_snapshot": {"close": 12.0},
        },
    ]
    screener_service.evaluate.return_value.rejected = []

    candidate_analysis_service = MagicMock()
    candidate_analysis_service.analyze_top_k.return_value = {
        "600519": {
            "ai_query_id": "q-600519",
            "ai_summary": "保持上升趋势。",
            "ai_operation_advice": "关注",
        }
    }
    market_data_sync_service = MagicMock()
    market_data_sync_service.fetcher_manager.get_market_stats.return_value = {
        "limit_up_count": 30, "limit_down_count": 10,
        "up_count": 2500, "down_count": 1500,
    }
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 2,
        "synced": 2,
        "skipped": 0,
        "errors": [],
    }
    notification_service = MagicMock()
    notification_service.notify_run.return_value = {
        "success": True,
        "notification_status": "sent",
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=screener_service,
        candidate_analysis_service=candidate_analysis_service,
        market_data_sync_service=market_data_sync_service,
        notification_service=notification_service,
    )
    service.config.screening_market_guard_enabled = False
    pipeline_candidates = [
        SimpleNamespace(
            code="600519",
            name="贵州茅台",
            rank=1,
            rule_score=91.0,
            rule_hits=["trend_aligned", "volume_expanding"],
            factor_snapshot={"close": 1500.0},
            matched_strategies=["trend_aligned"],
            strategy_scores={"trend_aligned": 91.0},
            setup_type="trend_breakout",
            entry_maturity="high",
            trade_stage="focus",
            market_regime="balanced",
            risk_level="medium",
            theme_position="main_theme",
            candidate_pool_level="focus_list",
        ),
        SimpleNamespace(
            code="000001",
            name="平安银行",
            rank=2,
            rule_score=80.0,
            rule_hits=["trend_aligned"],
            factor_snapshot={"close": 12.0},
            matched_strategies=["trend_aligned"],
            strategy_scores={"trend_aligned": 80.0},
            setup_type="trend_pullback",
            entry_maturity="medium",
            trade_stage="watch",
            market_regime="balanced",
            risk_level="medium",
            theme_position="secondary_theme",
            candidate_pool_level="watchlist",
        ),
    ]

    with patch("src.services.five_layer_pipeline.FiveLayerPipeline") as pipeline_cls:
        pipeline_cls.return_value.run.return_value = SimpleNamespace(
            candidates=pipeline_candidates,
            decision_context=None,
            pipeline_stats={
                "selected_after_limit": 2,
                "matched_before_limit": 2,
                "rejected_before_l345": 0,
            },
        )

        result = service.execute_run(
            trade_date=date(2026, 3, 13),
            stock_codes=None,
            candidate_limit=30,
            ai_top_k=1,
        )

    assert result["run_id"] == "run-001"
    assert result["status"] == "completed"

    statuses = [call.kwargs["status"] for call in db.update_screening_run_status.call_args_list]
    assert statuses == [
        "resolving_universe",
        "resolving_universe",
        "ingesting",
        "factorizing",
        "screening",
        "ai_enriching",
        "completed",
    ]
    completed_call = db.update_screening_run_status.call_args_list[-1]
    assert completed_call.kwargs["trade_date"] == date(2026, 3, 13)
    db.save_screening_candidates.assert_called_once()
    saved_candidates = db.save_screening_candidates.call_args.kwargs["candidates"]
    assert saved_candidates[0]["selected_for_ai"] is True
    assert saved_candidates[0]["ai_query_id"] == "q-600519"
    assert saved_candidates[1]["selected_for_ai"] is False
    candidate_analysis_service.analyze_top_k.assert_called_once()
    market_data_sync_service.sync_trade_date.assert_called_once()
    notification_service.notify_run.assert_called_once_with("run-001", force=True)


def test_screening_task_service_does_not_auto_notify_failed_run():
    notification_service = MagicMock()
    service = ScreeningTaskService.__new__(ScreeningTaskService)
    service._notification_service = notification_service

    service._notify_completed_run(run_id="run-failed", status="failed")

    notification_service.notify_run.assert_not_called()


def test_screening_task_service_auto_notifies_ai_degraded_run():
    notification_service = MagicMock()
    service = ScreeningTaskService.__new__(ScreeningTaskService)
    service._notification_service = notification_service

    service._notify_completed_run(
        run_id="run-ai-degraded",
        status="completed_with_ai_degraded",
    )

    notification_service.notify_run.assert_called_once_with("run-ai-degraded", force=True)


def test_screening_task_service_does_not_auto_notify_historical_random_sample():
    notification_service = MagicMock()
    service = ScreeningTaskService.__new__(ScreeningTaskService)
    service._notification_service = notification_service

    service._notify_completed_run(
        run_id="run-historical",
        status="completed",
        trigger_type="historical_random_sample",
    )

    notification_service.notify_run.assert_not_called()


def test_screening_task_service_auto_notify_failure_does_not_raise():
    notification_service = MagicMock()
    notification_service.notify_run.side_effect = RuntimeError("notify failed")
    service = ScreeningTaskService.__new__(ScreeningTaskService)
    service._notification_service = notification_service

    service._notify_completed_run(run_id="run-001", status="completed")

    notification_service.notify_run.assert_called_once_with("run-001", force=True)


def test_screening_task_service_uses_full_market_sync_for_manual_today_run():
    today = date.today()

    db = MagicMock()
    db.create_screening_run.return_value = "run-full-market-sync"
    db.get_screening_run.return_value = {
        "run_id": "run-full-market-sync",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 0,
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [
            {"code": "600519", "name": "贵州茅台"},
            {"code": "000001", "name": "平安银行"},
        ]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = today
    factor_service.build_factor_snapshot.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台", "close": 1500.0}]
    )

    screener_service = MagicMock()
    screener_service.evaluate.return_value.selected = []
    screener_service.evaluate.return_value.rejected = []

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": today.isoformat(),
        "total": 2,
        "synced": 2,
        "skipped": 0,
        "errors": [],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=screener_service,
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )
    service.config.screening_market_guard_enabled = False

    with patch.object(
        ScreeningTaskService,
        "_resolve_screening_trade_date",
        return_value=(today, None),
    ):
        result = service.execute_run(
            trade_date=today,
            stock_codes=None,
            candidate_limit=30,
            ai_top_k=0,
        )

    assert result["status"] == "completed"
    sync_kwargs = market_data_sync_service.sync_trade_date.call_args.kwargs
    assert sync_kwargs["trade_date"] == today
    assert sync_kwargs["stock_codes"] is None


def test_screening_task_service_skips_ai_analysis_when_ai_top_k_is_zero():
    db = MagicMock()
    db.create_screening_run.return_value = "run-no-ai"
    db.get_screening_run.return_value = {
        "run_id": "run-no-ai",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 1,
        "ai_top_k": 0,
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台"}]
    )

    factor_service = MagicMock()
    factor_service.build_factor_snapshot.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台", "close": 1500.0}]
    )

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 1,
        "synced": 1,
        "skipped": 0,
        "errors": [],
    }

    candidate_analysis_service = MagicMock()
    pipeline_candidates = [
        SimpleNamespace(
            code="600519",
            name="贵州茅台",
            rank=1,
            rule_score=91.0,
            rule_hits=["trend_aligned"],
            factor_snapshot={"close": 1500.0},
            matched_strategies=["trend_aligned"],
            strategy_scores={"trend_aligned": 91.0},
            setup_type="trend_breakout",
            entry_maturity="high",
            trade_stage="focus",
            market_regime="balanced",
            risk_level="medium",
            theme_position="main_theme",
            candidate_pool_level="focus_list",
        )
    ]

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        candidate_analysis_service=candidate_analysis_service,
        market_data_sync_service=market_data_sync_service,
    )
    service.config.screening_market_guard_enabled = False

    with patch("src.services.five_layer_pipeline.FiveLayerPipeline") as pipeline_cls:
        pipeline_cls.return_value.run.return_value = SimpleNamespace(
            candidates=pipeline_candidates,
            decision_context=None,
            pipeline_stats={"selected_after_limit": 1},
        )
        result = service.execute_run(
            trade_date=date(2026, 3, 13),
            candidate_limit=10,
            ai_top_k=0,
        )

    assert result["status"] == "completed"
    candidate_analysis_service.analyze_top_k.assert_not_called()
    saved_candidates = db.save_screening_candidates.call_args.kwargs["candidates"]
    assert saved_candidates[0]["selected_for_ai"] is False


def test_resolve_screening_trade_date_rolls_non_trading_day_back_to_previous_session():
    requested_trade_date = date(2026, 3, 15)
    market_now = datetime(2026, 3, 16, 15, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

    with patch("src.services.screening_task_service.is_market_open", side_effect=[False, False, True]):
        resolved_trade_date, warning = ScreeningTaskService._resolve_screening_trade_date(
            requested_trade_date=requested_trade_date,
            market="cn",
            market_now=market_now,
        )

    assert resolved_trade_date == date(2026, 3, 13)
    assert warning is not None
    assert "2026-03-15" in warning
    assert "2026-03-13" in warning


def test_resolve_screening_trade_date_blocks_today_before_market_close():
    requested_trade_date = date(2026, 3, 13)
    market_now = datetime(2026, 3, 13, 14, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

    with patch("src.services.screening_task_service.is_market_open", return_value=True):
        with pytest.raises(ScreeningTradeDateNotReadyError) as exc_info:
            ScreeningTaskService._resolve_screening_trade_date(
                requested_trade_date=requested_trade_date,
                market="cn",
                market_now=market_now,
            )

    assert exc_info.value.error_code == "screening_trade_time_not_ready"
    assert "15:00" in str(exc_info.value)


def test_screening_task_service_uses_previous_trading_date_when_requested_date_is_not_open():
    requested_trade_date = date(2026, 3, 15)
    resolved_trade_date = date(2026, 3, 13)

    db = MagicMock()
    db.create_screening_run.return_value = "run-prev-trading-date"
    db.get_screening_run.return_value = {
        "run_id": "run-prev-trading-date",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 0,
        "warnings": [f"所选日期 {requested_trade_date.isoformat()} 非交易日，已自动切换到最近交易日 {resolved_trade_date.isoformat()}"],
        "sync_failure_ratio": 0.0,
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台"}]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = resolved_trade_date
    factor_service.build_factor_snapshot.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台", "close": 1500.0}]
    )

    screener_service = MagicMock()
    screener_service.evaluate.return_value.selected = []
    screener_service.evaluate.return_value.rejected = []

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": resolved_trade_date.isoformat(),
        "total": 1,
        "synced": 1,
        "skipped": 0,
        "errors": [],
        "health_report": {},
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=screener_service,
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )
    service.config.screening_market_guard_enabled = False

    with patch.object(
        ScreeningTaskService,
        "_resolve_screening_trade_date",
        return_value=(
            resolved_trade_date,
            f"所选日期 {requested_trade_date.isoformat()} 非交易日，已自动切换到最近交易日 {resolved_trade_date.isoformat()}",
        ),
    ):
        result = service.execute_run(
            trade_date=requested_trade_date,
            stock_codes=None,
            candidate_limit=30,
            ai_top_k=0,
        )

    assert result["status"] == "completed"
    assert db.create_screening_run.call_args.kwargs["trade_date"] == resolved_trade_date
    assert market_data_sync_service.sync_trade_date.call_args.kwargs["trade_date"] == resolved_trade_date
    update_context_calls = db.update_screening_run_context.call_args_list
    assert any(
        call.kwargs["config_snapshot_updates"]["warnings"] == [
            f"所选日期 {requested_trade_date.isoformat()} 非交易日，已自动切换到最近交易日 {resolved_trade_date.isoformat()}"
        ]
        for call in update_context_calls
        if "warnings" in call.kwargs["config_snapshot_updates"]
    )


def test_screening_task_service_completes_with_ai_degraded_when_ai_analysis_fails():
    db = MagicMock()
    db.create_screening_run.return_value = "run-ai-degraded"
    db.get_screening_run.return_value = {
        "run_id": "run-ai-degraded",
        "mode": "balanced",
        "status": "completed_with_ai_degraded",
        "candidate_count": 1,
        "error_summary": "ai timeout",
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_service.build_factor_snapshot.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台", "close": 1500.0}]
    )

    screener_service = MagicMock()
    screener_service.evaluate.return_value.selected = [
        {
            "code": "600519",
            "name": "贵州茅台",
            "rank": 1,
            "rule_score": 91.0,
            "rule_hits": ["trend_aligned"],
            "factor_snapshot": {"close": 1500.0},
        }
    ]
    screener_service.evaluate.return_value.rejected = []

    candidate_analysis_service = MagicMock()
    candidate_analysis_service.analyze_top_k.side_effect = RuntimeError("ai timeout")

    market_data_sync_service = MagicMock()
    market_data_sync_service.fetcher_manager.get_market_stats.return_value = {
        "limit_up_count": 30, "limit_down_count": 10,
        "up_count": 2500, "down_count": 1500,
    }
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 1,
        "synced": 1,
        "skipped": 0,
        "errors": [],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=screener_service,
        candidate_analysis_service=candidate_analysis_service,
        market_data_sync_service=market_data_sync_service,
    )
    service.config.screening_market_guard_enabled = False
    pipeline_candidates = [
        SimpleNamespace(
            code="600519",
            name="贵州茅台",
            rank=1,
            rule_score=91.0,
            rule_hits=["trend_aligned"],
            factor_snapshot={"close": 1500.0},
            matched_strategies=["trend_aligned"],
            strategy_scores={"trend_aligned": 91.0},
            setup_type="trend_breakout",
            entry_maturity="high",
            trade_stage="focus",
            market_regime="balanced",
            risk_level="medium",
            theme_position="main_theme",
            candidate_pool_level="focus_list",
        )
    ]

    with patch("src.services.five_layer_pipeline.FiveLayerPipeline") as pipeline_cls:
        pipeline_cls.return_value.run.return_value = SimpleNamespace(
            candidates=pipeline_candidates,
            decision_context=None,
            pipeline_stats={
                "selected_after_limit": 1,
                "matched_before_limit": 1,
                "rejected_before_l345": 0,
            },
        )

        result = service.execute_run(
            trade_date=date(2026, 3, 13),
            candidate_limit=10,
            ai_top_k=1,
        )

    assert result["status"] == "completed_with_ai_degraded"
    completed_call = db.update_screening_run_status.call_args_list[-1]
    assert completed_call.kwargs["status"] == "completed_with_ai_degraded"
    assert completed_call.kwargs["error_summary"] == "ai timeout"
    saved_candidates = db.save_screening_candidates.call_args.kwargs["candidates"]
    assert saved_candidates[0]["ai_summary"] is None


def test_screening_task_service_completes_with_ai_degraded_when_partial_ai_failures_exist():
    db = MagicMock()
    db.create_screening_run.return_value = "run-ai-partial"
    db.get_screening_run.return_value = {
        "run_id": "run-ai-partial",
        "mode": "balanced",
        "status": "completed_with_ai_degraded",
        "candidate_count": 2,
        "error_summary": "AI degraded for: 000001",
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台"}, {"code": "000001", "name": "平安银行"}]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_service.build_factor_snapshot.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台"}, {"code": "000001", "name": "平安银行"}]
    )

    screener_service = MagicMock()
    screener_service.evaluate.return_value.selected = [
        {"code": "600519", "name": "贵州茅台", "rank": 1, "rule_score": 91.0, "rule_hits": [], "factor_snapshot": {}},
        {"code": "000001", "name": "平安银行", "rank": 2, "rule_score": 81.0, "rule_hits": [], "factor_snapshot": {}},
    ]
    screener_service.evaluate.return_value.rejected = []

    candidate_analysis_service = MagicMock()
    candidate_analysis_service.analyze_top_k.return_value = CandidateAnalysisBatchResult(
        results={
            "600519": {
                "ai_query_id": "query-600519",
                "ai_summary": "趋势延续。",
                "ai_operation_advice": "关注",
            }
        },
        failed_codes=["000001"],
    )

    market_data_sync_service = MagicMock()
    market_data_sync_service.fetcher_manager.get_market_stats.return_value = {
        "limit_up_count": 30, "limit_down_count": 10,
        "up_count": 2500, "down_count": 1500,
    }
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 2,
        "synced": 2,
        "skipped": 0,
        "errors": [],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=screener_service,
        candidate_analysis_service=candidate_analysis_service,
        market_data_sync_service=market_data_sync_service,
    )

    result = service.execute_run(trade_date=date(2026, 3, 13), candidate_limit=10, ai_top_k=2)

    assert result["status"] == "completed_with_ai_degraded"
    completed_call = db.update_screening_run_status.call_args_list[-1]
    assert completed_call.kwargs["status"] == "completed_with_ai_degraded"
    assert "000001" in completed_call.kwargs["error_summary"]


def test_screening_task_service_logs_stage_durations_and_health_report(caplog):
    db = MagicMock()
    db.create_screening_run.return_value = "run-observe"
    db.get_screening_run.return_value = {
        "run_id": "run-observe",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 1,
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_service.build_factor_snapshot.return_value = pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])

    screener_service = MagicMock()
    screener_service.evaluate.return_value.selected = [
        {"code": "600519", "name": "贵州茅台", "rank": 1, "rule_score": 91.0, "rule_hits": [], "factor_snapshot": {}}
    ]
    screener_service.evaluate.return_value.rejected = [{"code": "000001"}]

    market_data_sync_service = MagicMock()
    market_data_sync_service.fetcher_manager.get_market_stats.return_value = {
        "limit_up_count": 30, "limit_down_count": 10,
        "up_count": 2500, "down_count": 1500,
    }
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 1,
        "synced": 1,
        "skipped": 0,
        "errors": [],
        "health_report": {
            "summary": "ok",
            "success_rate": 1.0,
            "missing_count": 0,
            "error_count": 0,
        },
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=screener_service,
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )
    service.config.screening_market_guard_enabled = False
    pipeline_candidates = [
        SimpleNamespace(
            code="600519",
            name="贵州茅台",
            rank=1,
            rule_score=91.0,
            rule_hits=["trend_aligned"],
            factor_snapshot={},
            matched_strategies=["trend_aligned"],
            strategy_scores={"trend_aligned": 91.0},
            setup_type="trend_breakout",
            entry_maturity="high",
            trade_stage="focus",
            market_regime="balanced",
            risk_level="medium",
            theme_position="main_theme",
            candidate_pool_level="focus_list",
        )
    ]

    with patch("src.services.five_layer_pipeline.FiveLayerPipeline") as pipeline_cls:
        pipeline_cls.return_value.run.return_value = SimpleNamespace(
            candidates=pipeline_candidates,
            decision_context=None,
            pipeline_stats={
                "selected_after_limit": 1,
                "matched_before_limit": 2,
                "rejected_before_l345": 1,
            },
        )
        with caplog.at_level(logging.INFO, logger="src.services.screening_task_service"):
            service.execute_run(trade_date=date(2026, 3, 13), candidate_limit=10, ai_top_k=0)

    assert "screening_run_id=run-observe" in caplog.text
    assert "stage=ingesting" in caplog.text
    assert "duration_ms=" in caplog.text
    assert "health_summary=ok" in caplog.text
    assert "selected_count=1" in caplog.text
    assert "rejected_count=1" in caplog.text
    assert "event=run_completed" in caplog.text


def test_screening_task_service_logs_failed_stage_with_run_id(caplog):
    db = MagicMock()
    db.create_screening_run.return_value = "run-log-failed"
    db.get_screening_run.return_value = {
        "run_id": "run-log-failed",
        "mode": "balanced",
        "status": "failed",
        "candidate_count": 0,
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.side_effect = LocalUniverseNotReadyError("本地 instrument_master 为空")
    universe_service.sync_universe.side_effect = RuntimeError("sync failed")

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=MagicMock(),
        screener_service=MagicMock(),
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=MagicMock(),
    )

    with caplog.at_level(logging.ERROR, logger="src.services.screening_task_service"):
        result = service.execute_run(
            trade_date=date(2026, 3, 13),
            stock_codes=None,
            candidate_limit=30,
            ai_top_k=0,
        )

    assert result["status"] == "failed"
    assert "screening_run_id=run-log-failed" in caplog.text
    assert "stage=syncing_universe" in caplog.text
    assert "error=sync%20failed" in caplog.text


def test_screening_task_service_logs_health_report_before_partial_sync_failure(caplog):
    db = MagicMock()
    db.create_screening_run.return_value = "run-health-failed"
    db.get_screening_run.return_value = {
        "run_id": "run-health-failed",
        "mode": "balanced",
        "status": "failed",
        "candidate_count": 0,
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台"}, {"code": "000001", "name": "平安银行"}]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 2,
        "synced": 1,
        "skipped": 0,
        "errors": [{"code": "000001", "reason": "empty_data"}],
        "health_report": {
            "summary": "partial failure",
            "success_rate": 0.5,
            "missing_count": 1,
            "error_count": 1,
        },
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=MagicMock(),
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )

    with caplog.at_level(logging.INFO, logger="src.services.screening_task_service"):
        result = service.execute_run(
            trade_date=date(2026, 3, 13),
            stock_codes=None,
            candidate_limit=30,
            ai_top_k=0,
        )

    assert result["status"] == "failed"
    assert "screening_run_id=run-health-failed" in caplog.text
    assert "health_summary=partial%20failure" in caplog.text
    assert "health_success_rate=0.5" in caplog.text


def test_screening_task_service_creates_new_run_when_latest_matching_run_is_completed():
    db = MagicMock()
    db.find_latest_screening_run.return_value = {
        "run_id": "run-duplicate",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 2,
        "config_snapshot": {"stock_codes": ["000001", "600519"]},
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=MagicMock(),
        factor_service=MagicMock(),
        screener_service=MagicMock(),
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=MagicMock(),
    )
    db.create_screening_run.return_value = "run-new"
    db.get_screening_run.return_value = {
        "run_id": "run-new",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 0,
    }
    service._resolve_or_sync_universe = MagicMock(return_value=MagicMock(empty=True))

    result = service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=["600519", "000001"],
        candidate_limit=30,
        ai_top_k=5,
    )

    assert result["run_id"] == "run-new"
    assert result["status"] == "completed"
    db.create_screening_run.assert_called_once()


def test_screening_task_service_syncs_universe_when_local_master_missing():
    db = MagicMock()
    db.create_screening_run.return_value = "run-002"
    db.get_screening_run.return_value = {"run_id": "run-002", "mode": "balanced", "status": "completed", "candidate_count": 0}

    universe_service = MagicMock()
    universe_service.resolve_universe.side_effect = [
        LocalUniverseNotReadyError("本地 instrument_master 为空，请先执行 universe 同步"),
        pd.DataFrame([{"code": "600519", "name": "贵州茅台"}]),
    ]
    universe_service.sync_universe.return_value = {"saved_count": 1, "source": "StubFetcher", "market": "cn"}

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_service.build_factor_snapshot.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台", "close": 1500.0, "rule_score": 0}]
    )

    screener_service = MagicMock()
    screener_service.evaluate.return_value.selected = []
    screener_service.evaluate.return_value.rejected = []

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 1,
        "synced": 1,
        "skipped": 0,
        "errors": [],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=screener_service,
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )

    service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=None,
        candidate_limit=30,
        ai_top_k=0,
    )

    db.create_screening_run.assert_called_once()
    statuses = [call.kwargs["status"] for call in db.update_screening_run_status.call_args_list]
    assert statuses[:5] == [
        "resolving_universe",
        "syncing_universe",
        "resolving_universe",
        "ingesting",
        "factorizing",
    ]
    universe_service.sync_universe.assert_called_once_with(market="cn")


def test_screening_task_service_marks_run_failed_when_universe_sync_fails():
    db = MagicMock()
    db.create_screening_run.return_value = "run-003"
    db.get_screening_run.return_value = {"run_id": "run-003", "mode": "balanced", "status": "failed", "candidate_count": 0}

    universe_service = MagicMock()
    universe_service.resolve_universe.side_effect = LocalUniverseNotReadyError("本地 instrument_master 为空")
    universe_service.sync_universe.side_effect = RuntimeError("sync failed")

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=MagicMock(),
        screener_service=MagicMock(),
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=MagicMock(),
    )

    result = service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=None,
        candidate_limit=30,
        ai_top_k=0,
    )

    assert result["status"] == "failed"
    statuses = [call.kwargs["status"] for call in db.update_screening_run_status.call_args_list]
    assert statuses == ["resolving_universe", "syncing_universe", "failed"]
    failed_call = db.update_screening_run_status.call_args_list[-1]
    assert failed_call.kwargs["error_summary"] == "sync failed"


@patch("src.services.screening_task_service.get_config")
def test_screening_task_service_uses_config_defaults_when_limits_omitted(get_config_mock):
    get_config_mock.return_value.screening_default_mode = "balanced"
    get_config_mock.return_value.screening_candidate_limit = 1
    get_config_mock.return_value.screening_ai_top_k = 1
    get_config_mock.return_value.screening_min_list_days = 120
    get_config_mock.return_value.screening_min_volume_ratio = 1.2
    get_config_mock.return_value.screening_breakout_lookback_days = 20
    get_config_mock.return_value.screening_factor_lookback_days = 80

    db = MagicMock()
    db.create_screening_run.return_value = "run-004"
    db.get_screening_run.return_value = {"run_id": "run-004", "mode": "balanced", "status": "completed", "candidate_count": 1}

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [
            {"code": "600519", "name": "贵州茅台"},
            {"code": "000001", "name": "平安银行"},
        ]
    )

    factor_service = MagicMock()
    factor_service.lookback_days = 80
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_service.build_factor_snapshot.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台", "close": 1500.0, "rule_score": 0}]
    )

    screener_service = MagicMock()
    screener_service.min_list_days = 120
    screener_service.min_volume_ratio = 1.2
    screener_service.breakout_lookback_days = 20
    screener_service.evaluate.return_value.selected = [
        {"code": "600519", "name": "贵州茅台", "rank": 1, "rule_score": 91.0, "rule_hits": [], "factor_snapshot": {}},
        {"code": "000001", "name": "平安银行", "rank": 2, "rule_score": 80.0, "rule_hits": [], "factor_snapshot": {}},
    ]
    screener_service.evaluate.return_value.rejected = []

    candidate_analysis_service = MagicMock()
    candidate_analysis_service.analyze_top_k.return_value = {}

    market_data_sync_service = MagicMock()
    market_data_sync_service.fetcher_manager.get_market_stats.return_value = {
        "limit_up_count": 30, "limit_down_count": 10,
        "up_count": 2500, "down_count": 1500,
    }
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 2,
        "synced": 2,
        "skipped": 0,
        "errors": [],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=screener_service,
        candidate_analysis_service=candidate_analysis_service,
        market_data_sync_service=market_data_sync_service,
    )
    service.config.screening_market_guard_enabled = False
    pipeline_candidates = [
        SimpleNamespace(
            code="600519",
            name="贵州茅台",
            rank=1,
            rule_score=91.0,
            rule_hits=["trend_aligned"],
            factor_snapshot={},
            matched_strategies=["trend_aligned"],
            strategy_scores={"trend_aligned": 91.0},
            setup_type="trend_breakout",
            entry_maturity="high",
            trade_stage="focus",
            market_regime="balanced",
            risk_level="medium",
            theme_position="main_theme",
            candidate_pool_level="focus_list",
        )
    ]

    with patch("src.services.five_layer_pipeline.FiveLayerPipeline") as pipeline_cls:
        pipeline_cls.return_value.run.return_value = SimpleNamespace(
            candidates=pipeline_candidates,
            decision_context=None,
            pipeline_stats={
                "selected_after_limit": 1,
                "matched_before_limit": 1,
                "rejected_before_l345": 0,
            },
        )

        service.execute_run(
            trade_date=date(2026, 3, 13),
            stock_codes=None,
            candidate_limit=None,
            ai_top_k=None,
        )

    create_call = db.create_screening_run.call_args
    assert create_call.kwargs["config_snapshot"]["mode"] == "balanced"
    assert create_call.kwargs["config_snapshot"]["candidate_limit"] == 1
    assert create_call.kwargs["config_snapshot"]["ai_top_k"] == 1
    assert create_call.kwargs["config_snapshot"]["screening_min_list_days"] == 120
    assert create_call.kwargs["config_snapshot"]["screening_min_volume_ratio"] == 1.2
    assert (
        create_call.kwargs["config_snapshot"]["screening_breakout_lookback_days"]
        == 20
    )
    assert create_call.kwargs["config_snapshot"]["screening_factor_lookback_days"] == 80
    candidate_analysis_service.analyze_top_k.assert_called_once()
    assert candidate_analysis_service.analyze_top_k.call_args.kwargs["top_k"] == 1
    saved_candidates = db.save_screening_candidates.call_args.kwargs["candidates"]
    assert len(saved_candidates) == 1
    assert factor_service.build_factor_snapshot.call_args.kwargs["persist"] is False


def test_screening_task_service_reruns_failed_run_from_factorizing_stage():
    db = MagicMock()
    db.find_latest_screening_run.return_value = {
        "run_id": "run-failed",
        "mode": "balanced",
        "status": "failed",
        "candidate_count": 0,
        "config_snapshot": {"stock_codes": [], "next_resume_stage": "factorizing"},
    }
    db.get_screening_run.return_value = {
        "run_id": "run-failed",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 1,
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_service.build_factor_snapshot.return_value = pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])

    screener_service = MagicMock()
    screener_service.evaluate.return_value.selected = [
        {"code": "600519", "name": "贵州茅台", "rank": 1, "rule_score": 91.0, "rule_hits": [], "factor_snapshot": {}},
    ]
    screener_service.evaluate.return_value.rejected = []

    candidate_analysis_service = MagicMock()
    candidate_analysis_service.analyze_top_k.return_value = {}

    market_data_sync_service = MagicMock()
    market_data_sync_service.fetcher_manager.get_market_stats.return_value = {
        "limit_up_count": 30, "limit_down_count": 10,
        "up_count": 2500, "down_count": 1500,
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=screener_service,
        candidate_analysis_service=candidate_analysis_service,
        market_data_sync_service=market_data_sync_service,
    )

    result = service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=None,
        rerun_failed=True,
        resume_from="factorizing",
    )

    assert result["run_id"] == "run-failed"
    reset_call = db.reset_screening_run_for_rerun.call_args
    assert reset_call.kwargs["run_id"] == "run-failed"
    assert reset_call.kwargs["config_snapshot"]["next_resume_stage"] == "factorizing"
    db.create_screening_run.assert_not_called()
    market_data_sync_service.sync_trade_date.assert_not_called()
    factor_service.build_factor_snapshot.assert_called_once()
    statuses = [call.kwargs["status"] for call in db.update_screening_run_status.call_args_list]
    assert statuses == ["factorizing", "screening", "ai_enriching", "completed"]


def test_screening_task_service_creates_new_run_without_explicit_trade_date_when_latest_match_is_completed():
    db = MagicMock()
    db.find_latest_screening_run.return_value = {
        "run_id": "run-latest-trading-day",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 3,
        "config_snapshot": {
            "requested_trade_date": "2026-03-15",
            "stock_codes": [],
            "candidate_limit": 30,
            "ai_top_k": 5,
            "screening_min_list_days": 120,
            "screening_min_volume_ratio": 1.2,
            "screening_breakout_lookback_days": 20,
            "screening_factor_lookback_days": 80,
            "mode": "balanced",
        },
        "trade_date": "2026-03-13",
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=MagicMock(),
        factor_service=MagicMock(),
        screener_service=MagicMock(),
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=MagicMock(),
    )
    db.create_screening_run.return_value = "run-new-latest-trading-day"
    db.get_screening_run.return_value = {
        "run_id": "run-new-latest-trading-day",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 0,
    }
    service._resolve_or_sync_universe = MagicMock(return_value=MagicMock(empty=True))
    service.config.screening_market_guard_enabled = False

    with patch("src.services.screening_task_service.date") as date_mock:
        date_mock.today.return_value = date(2026, 3, 15)
        result = service.execute_run(trade_date=None)

    assert result["run_id"] == "run-new-latest-trading-day"
    db.create_screening_run.assert_called_once()


def test_screening_task_service_uses_latest_passed_audit_trade_date_before_factor_window_fallback():
    db = MagicMock()
    db.create_screening_run.return_value = "run-latest-audit-truth"
    db.get_screening_run.return_value = {
        "run_id": "run-latest-audit-truth",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 0,
    }
    db.get_latest_passed_kline_audit_trade_date.return_value = SimpleNamespace(
        market="cn",
        trade_date=date(2026, 3, 13),
        pass_status="passed",
    )
    db.get_kline_audit_trade_date.return_value = SimpleNamespace(
        market="cn",
        trade_date=date(2026, 3, 13),
        pass_status="passed",
    )

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = None
    factor_service.build_factor_snapshot.return_value = pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 1,
        "synced": 1,
        "skipped": 0,
        "errors": [],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=MagicMock(),
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )
    service.config.screening_market_guard_enabled = False

    with patch("src.services.five_layer_pipeline.FiveLayerPipeline") as pipeline_cls:
        pipeline_cls.return_value.run.return_value = SimpleNamespace(
            candidates=[],
            decision_context=None,
            pipeline_stats={
                "selected_after_limit": 0,
                "matched_before_limit": 0,
                "rejected_before_l345": 0,
            },
        )
        result = service.execute_run(trade_date=None, candidate_limit=30, ai_top_k=0)

    assert result["status"] == "completed"
    factor_service.get_latest_trade_date.assert_not_called()
    factor_service.build_factor_snapshot.assert_called_once()
    assert factor_service.build_factor_snapshot.call_args.kwargs["trade_date"] == date(2026, 3, 13)


def test_screening_task_service_treats_different_effective_config_as_new_run():
    db = MagicMock()
    db.find_latest_screening_run.return_value = None
    db.create_screening_run.return_value = "run-config-new"
    db.get_screening_run.return_value = {
        "run_id": "run-config-new",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 0,
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_service.build_factor_snapshot.return_value = pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])

    screener_service = MagicMock()
    screener_service.evaluate.return_value.selected = []
    screener_service.evaluate.return_value.rejected = []

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 1,
        "synced": 1,
        "skipped": 0,
        "errors": [],
        "health_report": {
            "expected_count": 1,
            "available_count": 1,
            "missing_count": 0,
            "error_count": 0,
            "missing_codes": [],
            "success_rate": 1.0,
            "summary": "ok",
        },
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=screener_service,
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )

    service.execute_run(
        trade_date=date(2026, 3, 13),
        candidate_limit=20,
        ai_top_k=1,
    )

    create_call = db.create_screening_run.call_args
    assert create_call.kwargs["config_snapshot"]["candidate_limit"] == 20
    assert create_call.kwargs["config_snapshot"]["ai_top_k"] == 1


def test_screening_task_service_rejects_factorizing_rerun_when_previous_sync_not_complete():
    db = MagicMock()
    db.find_latest_screening_run.return_value = {
        "run_id": "run-failed",
        "mode": "balanced",
        "status": "failed",
        "candidate_count": 0,
        "config_snapshot": {
            "stock_codes": [],
            "next_resume_stage": "ingesting",
        },
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=MagicMock(),
        factor_service=MagicMock(),
        screener_service=MagicMock(),
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=MagicMock(),
    )

    with pytest.raises(ValueError, match="factorizing"):
        service.execute_run(
            trade_date=date(2026, 3, 13),
            rerun_failed=True,
            resume_from="factorizing",
        )


def test_screening_task_service_refreshes_snapshot_when_failed_run_is_rerun():
    db = MagicMock()
    db.find_latest_screening_run.return_value = {
        "run_id": "run-failed",
        "mode": "balanced",
        "status": "failed",
        "candidate_count": 0,
        "config_snapshot": {
            "stock_codes": [],
            "candidate_limit": 30,
            "ai_top_k": 5,
            "next_resume_stage": "factorizing",
        },
    }
    db.get_screening_run.return_value = {
        "run_id": "run-failed",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 0,
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_service.build_factor_snapshot.return_value = pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])

    screener_service = MagicMock()
    screener_service.evaluate.return_value.selected = []
    screener_service.evaluate.return_value.rejected = []

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=screener_service,
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=MagicMock(),
    )

    service.execute_run(
        trade_date=date(2026, 3, 13),
        rerun_failed=True,
        resume_from="factorizing",
        candidate_limit=10,
        ai_top_k=1,
    )

    reset_call = db.reset_screening_run_for_rerun.call_args
    assert reset_call.kwargs["run_id"] == "run-failed"
    assert reset_call.kwargs["ai_top_k"] == 1
    assert reset_call.kwargs["config_snapshot"]["candidate_limit"] == 10


def test_screening_task_service_returns_existing_run_when_rerun_claim_is_lost():
    db = MagicMock()
    db.find_latest_screening_run.return_value = {
        "run_id": "run-failed",
        "mode": "balanced",
        "status": "failed",
        "candidate_count": 0,
        "config_snapshot": {
            "stock_codes": [],
            "next_resume_stage": "factorizing",
        },
    }
    db.reset_screening_run_for_rerun.return_value = False
    db.get_screening_run.return_value = {
        "run_id": "run-failed",
        "mode": "balanced",
        "status": "pending",
        "candidate_count": 0,
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=MagicMock(),
        factor_service=MagicMock(),
        screener_service=MagicMock(),
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=MagicMock(),
    )

    result = service.execute_run(
        trade_date=date(2026, 3, 13),
        rerun_failed=True,
        resume_from="factorizing",
    )

    assert result["run_id"] == "run-failed"
    assert result["status"] == "pending"


def test_screening_task_service_returns_existing_run_when_failed_rerun_already_claimed():
    db = MagicMock()
    db.find_latest_screening_run.return_value = {
        "run_id": "run-claimed",
        "mode": "balanced",
        "status": "pending",
        "candidate_count": 0,
        "config_snapshot": {
            "stock_codes": [],
            "next_resume_stage": "factorizing",
        },
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=MagicMock(),
        factor_service=MagicMock(),
        screener_service=MagicMock(),
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=MagicMock(),
    )

    result = service.execute_run(
        trade_date=date(2026, 3, 13),
        rerun_failed=True,
        resume_from="factorizing",
    )

    assert result["run_id"] == "run-claimed"
    assert result["status"] == "pending"


def test_get_run_recovers_stale_non_terminal_run_before_returning():
    db = MagicMock()
    stale_run = {
        "run_id": "run-stale-get",
        "mode": "balanced",
        "status": "screening",
        "candidate_count": 0,
        "started_at": datetime(2026, 3, 13, 10, 0),
        "last_activity_at": datetime(2026, 3, 13, 10, 5),
        "config_snapshot": {},
    }
    recovered_run = {
        "run_id": "run-stale-get",
        "mode": "balanced",
        "status": "failed",
        "candidate_count": 0,
        "error_summary": "任务超时",
        "config_snapshot": {},
    }
    db.get_screening_run.side_effect = [stale_run, recovered_run]

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=MagicMock(),
        factor_service=MagicMock(),
        screener_service=MagicMock(),
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=MagicMock(),
    )

    with patch.object(service, "_recover_stale_run", return_value=None) as recover_mock:
        result = service.get_run("run-stale-get")

    recover_mock.assert_called_once_with(stale_run)
    assert db.get_screening_run.call_count == 2
    assert result is not None
    assert result["run_id"] == "run-stale-get"
    assert result["status"] == "failed"


def test_list_runs_recovers_stale_non_terminal_runs_before_returning():
    db = MagicMock()
    stale_run = {
        "run_id": "run-stale-list",
        "mode": "balanced",
        "status": "ingesting",
        "candidate_count": 0,
        "started_at": datetime(2026, 3, 13, 10, 0),
        "last_activity_at": datetime(2026, 3, 13, 10, 5),
        "config_snapshot": {},
    }
    completed_run = {
        "run_id": "run-completed",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 1,
        "config_snapshot": {},
    }
    refreshed_stale_run = {
        "run_id": "run-stale-list",
        "mode": "balanced",
        "status": "failed",
        "candidate_count": 0,
        "error_summary": "任务超时",
        "config_snapshot": {},
    }
    db.list_screening_runs.return_value = [stale_run, completed_run]
    db.get_screening_run.return_value = refreshed_stale_run

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=MagicMock(),
        factor_service=MagicMock(),
        screener_service=MagicMock(),
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=MagicMock(),
    )

    with patch.object(service, "_recover_stale_run", side_effect=[None, completed_run]) as recover_mock:
        result = service.list_runs(limit=20)

    assert recover_mock.call_args_list[0].args[0] == stale_run
    assert recover_mock.call_args_list[1].args[0] == completed_run
    db.get_screening_run.assert_called_once_with("run-stale-list")
    assert [item["run_id"] for item in result] == ["run-stale-list", "run-completed"]
    assert result[0]["status"] == "failed"
    assert result[1]["status"] == "completed"


@patch("src.services.screening_task_service.get_config")
@patch("src.services.screening_task_service.FactorService")
@patch("src.services.screening_task_service.ScreenerService")
def test_screening_task_service_builds_mode_specific_services(
    screener_cls,
    factor_cls,
    get_config_mock,
):
    get_config_mock.return_value.screening_default_mode = "balanced"
    get_config_mock.return_value.screening_candidate_limit = 30
    get_config_mock.return_value.screening_ai_top_k = 5
    get_config_mock.return_value.screening_min_list_days = 120
    get_config_mock.return_value.screening_min_volume_ratio = 1.2
    get_config_mock.return_value.screening_breakout_lookback_days = 20
    get_config_mock.return_value.screening_factor_lookback_days = 80

    db = MagicMock()
    db.create_screening_run.return_value = "run-004a"
    db.get_screening_run.return_value = {"run_id": "run-004a", "mode": "aggressive", "status": "completed", "candidate_count": 0}

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])

    screener_instance = MagicMock()
    screener_instance.min_list_days = 60
    screener_instance.min_volume_ratio = 1.0
    screener_instance.breakout_lookback_days = 30
    screener_instance.evaluate.return_value.selected = []
    screener_instance.evaluate.return_value.rejected = []
    screener_cls.return_value = screener_instance

    factor_instance = MagicMock()
    factor_instance.lookback_days = 100
    factor_instance.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_instance.build_factor_snapshot.return_value = pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])
    factor_cls.return_value = factor_instance

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 1,
        "synced": 1,
        "skipped": 0,
        "errors": [],
    }
    market_data_sync_service.fetcher_manager.get_market_stats.return_value = {
        "limit_up_count": 30, "limit_down_count": 10,
        "up_count": 2500, "down_count": 1500,
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=None,
        screener_service=None,
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )

    service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=None,
        mode="aggressive",
        candidate_limit=None,
        ai_top_k=None,
    )

    screener_cls.assert_called_once_with(
        min_list_days=60,
        min_volume_ratio=1.0,
        breakout_lookback_days=15,
        skill_manager=None,
        strategy_names=None,
    )
    factor_cls.assert_called_once_with(
        db,
        lookback_days=60,
        breakout_lookback_days=15,
        min_list_days=60,
    )
    assert factor_instance.build_factor_snapshot.call_args.kwargs["persist"] is False


@patch("src.services.screening_task_service.get_config")
@patch("src.services.screening_task_service.FactorService")
def test_screening_task_service_does_not_inject_theme_context_into_runtime_factor_service(
    factor_cls,
    get_config_mock,
):
    get_config_mock.return_value.screening_default_mode = "balanced"
    get_config_mock.return_value.screening_candidate_limit = 30
    get_config_mock.return_value.screening_ai_top_k = 5
    get_config_mock.return_value.screening_min_list_days = 120
    get_config_mock.return_value.screening_min_volume_ratio = 1.2
    get_config_mock.return_value.screening_breakout_lookback_days = 20
    get_config_mock.return_value.screening_factor_lookback_days = 80

    service = ScreeningTaskService(
        db_manager=MagicMock(),
        universe_service=MagicMock(),
        factor_service=None,
        screener_service=MagicMock(),
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=MagicMock(),
    )
    service._theme_context = MagicMock(
        themes=[MagicMock(name="机器人"), MagicMock(name="AI Agent")]
    )

    runtime_config = service.resolve_run_config(mode="balanced", candidate_limit=30, ai_top_k=5)
    service._build_runtime_factor_service(runtime_config)

    factor_cls.assert_called_once_with(
        service.db,
        lookback_days=80,
        breakout_lookback_days=20,
        min_list_days=120,
    )


def test_build_theme_pipeline_context_updates_combines_local_and_external():
    decision_context = {
        "sector_heat_results": [
            {
                "board_name": "AI芯片",
                "canonical_theme": "AI芯片",
                "sector_hot_score": 92.0,
                "sector_status": "hot",
                "sector_stage": "main_rise",
                "stock_count": 18,
                "up_count": 12,
                "limit_up_count": 3,
            },
            {
                "board_name": "机器人",
                "canonical_theme": "机器人",
                "sector_hot_score": 76.0,
                "sector_status": "warm",
                "sector_stage": "expand",
                "stock_count": 20,
                "up_count": 10,
                "limit_up_count": 1,
            },
        ],
        "hot_theme_count": 1,
        "warm_theme_count": 1,
    }
    theme_context = OpenClawThemeContext(
        source="openclaw",
        trade_date="2026-03-27",
        market="cn",
        themes=[
            ExternalTheme(
                name="AI芯片",
                heat_score=90.0,
                confidence=0.9,
                catalyst_summary="政策催化",
                keywords=["AI", "芯片", "算力"],
                evidence=[],
            )
        ],
        accepted_at="2026-03-27T15:00:00",
    )

    updates = ScreeningTaskService._build_theme_pipeline_context_updates(
        decision_context=decision_context,
        trade_date=date(2026, 3, 27),
        market="cn",
        theme_context=theme_context,
    )

    assert updates["local_theme_pipeline"]["source"] == "local"
    assert updates["local_theme_pipeline"]["selected_theme_names"] == ["AI芯片", "机器人概念"]
    assert updates["external_theme_pipeline"]["source"] == "openclaw"
    assert updates["fused_theme_pipeline"]["active_sources"] == ["local", "external"]
    assert updates["fused_theme_pipeline"]["selected_theme_names"] == ["AI芯片", "机器人概念"]
    assert updates["fused_theme_pipeline"]["merged_theme_count"] == 2
    assert updates["fused_theme_pipeline"]["merged_themes"][0]["matched_sources"] == ["local", "external"]


def test_enrich_run_payload_exposes_theme_pipeline_snapshots():
    payload = {
        "run_id": "run-theme-pipeline",
        "status": "completed",
        "config_snapshot": {
            "local_theme_pipeline": {
                "source": "local",
                "selected_theme_names": ["AI芯片", "机器人概念"],
            },
            "external_theme_pipeline": {
                "source": "openclaw",
                "top_theme_names": ["AI芯片"],
            },
            "fused_theme_pipeline": {
                "active_sources": ["local", "external"],
                "selected_theme_names": ["AI芯片", "机器人概念"],
                "merged_theme_count": 2,
            },
        },
    }

    enriched = ScreeningTaskService._enrich_run_payload(payload)

    assert enriched["local_theme_pipeline"]["source"] == "local"
    assert enriched["external_theme_pipeline"]["source"] == "openclaw"
    assert enriched["fused_theme_pipeline"]["merged_theme_count"] == 2


def test_screening_task_service_persists_decision_context_when_theme_pipeline_build_fails():
    db = MagicMock()
    db.create_screening_run.return_value = "run-theme-context"
    db.get_screening_run.return_value = {
        "run_id": "run-theme-context",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 1,
    }
    db.update_screening_run_context.return_value = True

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_service.build_factor_snapshot.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台", "close": 1500.0}]
    )

    screener_service = MagicMock()
    screener_service.evaluate.return_value.selected = []
    screener_service.evaluate.return_value.rejected = []

    market_data_sync_service = MagicMock()
    market_data_sync_service.fetcher_manager.get_market_stats.return_value = {
        "limit_up_count": 30,
        "limit_down_count": 10,
        "up_count": 2500,
        "down_count": 1500,
    }
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 1,
        "synced": 1,
        "skipped": 0,
        "errors": [],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=screener_service,
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )
    service.config.screening_market_guard_enabled = False

    with patch.object(
        ScreeningTaskService,
        "_build_theme_pipeline_context_updates",
        side_effect=RuntimeError("theme pipeline failed"),
    ):
        result = service.execute_run(
            trade_date=date(2026, 3, 13),
            candidate_limit=10,
            ai_top_k=0,
        )

    assert result["status"] == "completed"
    assert any(
        "decision_context" in call.kwargs["config_snapshot_updates"]
        for call in db.update_screening_run_context.call_args_list
    )


@patch("src.services.screening_task_service.get_config")
def test_screening_task_service_rejects_non_balanced_mode_with_custom_services(get_config_mock):
    get_config_mock.return_value.screening_default_mode = "balanced"
    get_config_mock.return_value.screening_candidate_limit = 30
    get_config_mock.return_value.screening_ai_top_k = 5
    get_config_mock.return_value.screening_min_list_days = 120
    get_config_mock.return_value.screening_min_volume_ratio = 1.2
    get_config_mock.return_value.screening_breakout_lookback_days = 20
    get_config_mock.return_value.screening_factor_lookback_days = 80

    service = ScreeningTaskService(
        db_manager=MagicMock(),
        universe_service=MagicMock(),
        factor_service=MagicMock(),
        screener_service=MagicMock(),
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=MagicMock(),
    )

    with pytest.raises(ValueError, match="balanced"):
        service.execute_run(mode="aggressive")


@patch("src.services.screening_task_service.get_config")
@patch("src.services.screening_task_service.FactorService")
@patch("src.services.screening_task_service.ScreenerService")
def test_screening_task_service_disables_shared_snapshot_persistence_for_custom_stock_scope(
    screener_cls,
    factor_cls,
    get_config_mock,
):
    get_config_mock.return_value.screening_default_mode = "balanced"
    get_config_mock.return_value.screening_candidate_limit = 30
    get_config_mock.return_value.screening_ai_top_k = 5
    get_config_mock.return_value.screening_min_list_days = 120
    get_config_mock.return_value.screening_min_volume_ratio = 1.2
    get_config_mock.return_value.screening_breakout_lookback_days = 20
    get_config_mock.return_value.screening_factor_lookback_days = 80

    db = MagicMock()
    db.create_screening_run.return_value = "run-004b"
    db.get_screening_run.return_value = {"run_id": "run-004b", "mode": "balanced", "status": "completed", "candidate_count": 0}

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])

    screener_instance = MagicMock()
    screener_instance.evaluate.return_value.selected = []
    screener_instance.evaluate.return_value.rejected = []
    screener_cls.return_value = screener_instance

    factor_instance = MagicMock()
    factor_instance.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_instance.build_factor_snapshot.return_value = pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])
    factor_cls.return_value = factor_instance

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 1,
        "synced": 1,
        "skipped": 0,
        "errors": [],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=None,
        screener_service=None,
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )

    service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=["600519"],
        mode=None,
        candidate_limit=None,
        ai_top_k=None,
    )

    assert factor_instance.build_factor_snapshot.call_args.kwargs["persist"] is False


@patch("src.services.screening_task_service.get_config")
@patch("src.services.screening_task_service.FactorService")
@patch("src.services.screening_task_service.ScreenerService")
def test_screening_task_service_persists_shared_snapshot_only_for_default_balanced_full_market(
    screener_cls,
    factor_cls,
    get_config_mock,
):
    get_config_mock.return_value.screening_default_mode = "balanced"
    get_config_mock.return_value.screening_candidate_limit = 30
    get_config_mock.return_value.screening_ai_top_k = 5
    get_config_mock.return_value.screening_min_list_days = 120
    get_config_mock.return_value.screening_min_volume_ratio = 1.2
    get_config_mock.return_value.screening_breakout_lookback_days = 20
    get_config_mock.return_value.screening_factor_lookback_days = 80

    db = MagicMock()
    db.create_screening_run.return_value = "run-004c"
    db.get_screening_run.return_value = {"run_id": "run-004c", "mode": "balanced", "status": "completed", "candidate_count": 0}

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])

    screener_instance = MagicMock()
    screener_instance.evaluate.return_value.selected = []
    screener_instance.evaluate.return_value.rejected = []
    screener_cls.return_value = screener_instance

    factor_instance = MagicMock()
    factor_instance.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_instance.build_factor_snapshot.return_value = pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])
    factor_cls.return_value = factor_instance

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 1,
        "synced": 1,
        "skipped": 0,
        "errors": [],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=None,
        screener_service=None,
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )

    service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=None,
        mode=None,
        candidate_limit=None,
        ai_top_k=None,
    )

    assert factor_instance.build_factor_snapshot.call_args.kwargs["persist"] is True


def test_screening_task_service_fails_when_status_persistence_is_rejected():
    db = MagicMock()
    db.create_screening_run.return_value = "run-005"
    db.get_screening_run.return_value = {"run_id": "run-005", "mode": "balanced", "status": "failed", "candidate_count": 0}

    def _update_status(**kwargs):
        return kwargs["status"] != "ingesting"

    db.update_screening_run_status.side_effect = _update_status

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)

    market_data_sync_service = MagicMock()

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=MagicMock(),
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )

    result = service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=None,
        candidate_limit=30,
        ai_top_k=0,
    )

    assert result["status"] == "failed"
    market_data_sync_service.sync_trade_date.assert_not_called()
    failed_call = db.update_screening_run_status.call_args_list[-1]
    assert "状态更新失败" in failed_call.kwargs["error_summary"]


def test_screening_task_service_fails_when_market_sync_has_partial_errors():
    db = MagicMock()
    db.create_screening_run.return_value = "run-006"
    db.get_screening_run.return_value = {"run_id": "run-006", "mode": "balanced", "status": "failed", "candidate_count": 0}

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台"}, {"code": "000001", "name": "平安银行"}]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 2,
        "synced": 1,
        "skipped": 0,
        "errors": [{"code": "000001", "reason": "empty_data"}],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=MagicMock(),
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )

    result = service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=None,
        candidate_limit=30,
        ai_top_k=0,
    )

    assert result["status"] == "failed"
    statuses = [call.kwargs["status"] for call in db.update_screening_run_status.call_args_list]
    assert statuses == ["resolving_universe", "resolving_universe", "ingesting", "failed"]
    failed_call = db.update_screening_run_status.call_args_list[-1]
    assert "000001" in failed_call.kwargs["error_summary"]


def test_screening_task_service_uses_error_detail_in_sync_failure_summary():
    db = MagicMock()
    db.create_screening_run.return_value = "run-006-detail"
    db.get_screening_run.return_value = {"run_id": "run-006-detail", "mode": "balanced", "status": "failed", "candidate_count": 0}

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台"}, {"code": "000001", "name": "平安银行"}]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 2,
        "synced": 1,
        "skipped": 0,
        "errors": [
            {
                "code": "000001",
                "reason": "empty_data",
                "detail": "所有数据源获取 000001 失败: provider timeout chain",
            }
        ],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=MagicMock(),
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )

    result = service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=None,
        candidate_limit=30,
        ai_top_k=0,
    )

    assert result["status"] == "failed"
    failed_call = db.update_screening_run_status.call_args_list[-1]
    assert "provider timeout chain" in failed_call.kwargs["error_summary"]


def test_screening_task_service_ignores_delisted_sync_errors_and_continues():
    db = MagicMock()
    db.create_screening_run.return_value = "run-006b"
    db.get_screening_run.return_value = {
        "run_id": "run-006b",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 1,
        "error_summary": "已跳过同步失败股票: 000002",
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [
            {"code": "600519", "name": "贵州茅台", "listing_status": "active"},
            {"code": "000002", "name": "万科A退", "listing_status": "active"},
        ]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_service.build_factor_snapshot.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台", "close": 1500.0}]
    )

    screener_service = MagicMock()
    screener_service.evaluate.return_value.selected = []
    screener_service.evaluate.return_value.rejected = []

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 2,
        "synced": 1,
        "skipped": 0,
        "errors": [{"code": "000002", "reason": "empty_data"}],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=screener_service,
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )
    service.config.screening_ingest_failure_threshold = 0.8
    service.config.screening_market_guard_enabled = False

    result = service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=None,
        candidate_limit=30,
        ai_top_k=0,
    )

    assert result["status"] == "completed"
    factor_universe = factor_service.build_factor_snapshot.call_args.kwargs["universe_df"]
    assert factor_universe["code"].tolist() == ["600519"]
    completed_call = db.update_screening_run_status.call_args_list[-1]
    assert completed_call.kwargs["status"] == "completed"
    assert completed_call.kwargs["error_summary"] == "已跳过同步失败股票: 000002"


def test_screening_task_service_continues_with_skippable_sync_failures_below_threshold():
    db = MagicMock()
    db.create_screening_run.return_value = "run-006-threshold-ok"
    db.get_screening_run.return_value = {
        "run_id": "run-006-threshold-ok",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 1,
        "config_snapshot": {
            "failed_symbols": ["000002"],
            "warnings": ["已跳过同步失败股票: 000002"],
            "sync_failure_ratio": 0.25,
        },
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [
            {"code": "600519", "name": "贵州茅台", "listing_status": "active"},
            {"code": "000001", "name": "平安银行", "listing_status": "active"},
            {"code": "000002", "name": "未知股票", "listing_status": "active"},
            {"code": "000003", "name": "招商银行", "listing_status": "active"},
        ]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_service.build_factor_snapshot.return_value = pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])

    screener_service = MagicMock()
    screener_service.evaluate.return_value.selected = []
    screener_service.evaluate.return_value.rejected = []

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 4,
        "synced": 3,
        "skipped": 0,
        "errors": [
            {
                "code": "000002",
                "reason": "fetch_failed",
                "reason_class": "skip_eligible",
                "detail": "all providers failed",
            }
        ],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=screener_service,
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )
    service.config.screening_ingest_failure_threshold = 0.3

    result = service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=None,
        candidate_limit=30,
        ai_top_k=0,
    )

    assert result["status"] == "completed"
    assert result["failed_symbols"] == ["000002"]
    assert result["warnings"] == ["已跳过同步失败股票: 000002"]
    assert result["sync_failure_ratio"] == 0.25
    factor_universe = factor_service.build_factor_snapshot.call_args.kwargs["universe_df"]
    assert factor_universe["code"].tolist() == ["600519", "000001", "000003"]


def test_screening_task_service_fails_when_skippable_sync_failure_ratio_exceeds_threshold():
    db = MagicMock()
    db.create_screening_run.return_value = "run-006-threshold-failed"
    db.get_screening_run.return_value = {
        "run_id": "run-006-threshold-failed",
        "mode": "balanced",
        "status": "failed",
        "candidate_count": 0,
        "config_snapshot": {
            "failed_symbols": ["000002", "000003"],
            "warnings": ["已跳过同步失败股票: 000002, 000003"],
            "sync_failure_ratio": 0.5,
        },
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [
            {"code": "600519", "name": "贵州茅台", "listing_status": "active"},
            {"code": "000001", "name": "平安银行", "listing_status": "active"},
            {"code": "000002", "name": "未知股票A", "listing_status": "active"},
            {"code": "000003", "name": "未知股票B", "listing_status": "active"},
        ]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 4,
        "synced": 2,
        "skipped": 0,
        "errors": [
            {
                "code": "000002",
                "reason": "fetch_failed",
                "reason_class": "skip_eligible",
                "detail": "all providers failed",
            },
            {"code": "000003", "reason": "empty_data", "detail": "no data"},
        ],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=MagicMock(),
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )
    service.config.screening_ingest_failure_threshold = 0.4

    result = service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=None,
        candidate_limit=30,
        ai_top_k=0,
    )

    assert result["status"] == "failed"
    factor_service.build_factor_snapshot.assert_not_called()
    failed_call = db.update_screening_run_status.call_args_list[-1]
    assert "同步失败比例" in failed_call.kwargs["error_summary"]


def test_screening_ingest_does_not_silently_accept_fetch_failed_as_success():
    db = MagicMock()
    db.create_screening_run.return_value = "run-fetch-failed-blocking"
    db.get_screening_run.return_value = {
        "run_id": "run-fetch-failed-blocking",
        "mode": "balanced",
        "status": "failed",
        "candidate_count": 0,
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [
            {"code": "600519", "name": "贵州茅台", "listing_status": "active"},
            {"code": "000001", "name": "平安银行", "listing_status": "active"},
        ]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 2,
        "synced": 1,
        "skipped": 0,
        "errors": [
            {
                "code": "000001",
                "reason": "fetch_failed",
                "reason_class": "blocking",
                "detail": "all providers failed",
            }
        ],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=MagicMock(),
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )
    service.config.screening_ingest_failure_threshold = 0.8
    service.config.screening_market_guard_enabled = False

    result = service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=None,
        candidate_limit=30,
        ai_top_k=0,
    )

    assert result["status"] == "failed"
    factor_service.build_factor_snapshot.assert_not_called()
    failed_call = db.update_screening_run_status.call_args_list[-1]
    assert "000001: all providers failed" in failed_call.kwargs["error_summary"]


def test_screening_task_service_fails_when_trade_date_audit_not_passed():
    db = MagicMock()
    db.create_screening_run.return_value = "run-trade-date-audit-blocked"
    db.get_screening_run.return_value = {
        "run_id": "run-trade-date-audit-blocked",
        "mode": "balanced",
        "status": "failed",
        "candidate_count": 0,
    }
    db.get_kline_audit_trade_date.return_value = SimpleNamespace(
        market="cn",
        trade_date=date(2026, 3, 13),
        pass_status="degraded",
    )

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台", "listing_status": "active"}]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_service.build_factor_snapshot.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台"}]
    )

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 1,
        "synced": 1,
        "skipped": 0,
        "errors": [],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=MagicMock(),
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )
    service.config.screening_market_guard_enabled = False

    result = service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=None,
        candidate_limit=30,
        ai_top_k=0,
    )

    assert result["status"] == "failed"
    db.get_kline_audit_trade_date.assert_called_once_with(
        market="cn",
        trade_date=date(2026, 3, 13),
    )
    factor_service.build_factor_snapshot.assert_not_called()
    failed_call = db.update_screening_run_status.call_args_list[-1]
    assert "pass_status=degraded" in failed_call.kwargs["error_summary"]


def test_screening_task_service_fails_when_trade_date_audit_record_is_missing():
    db = MagicMock()
    db.create_screening_run.return_value = "run-trade-date-audit-missing"
    db.get_screening_run.return_value = {
        "run_id": "run-trade-date-audit-missing",
        "mode": "balanced",
        "status": "failed",
        "candidate_count": 0,
    }
    db.get_kline_audit_trade_date.return_value = None

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台", "listing_status": "active"}]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_service.build_factor_snapshot.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台"}]
    )

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 1,
        "synced": 1,
        "skipped": 0,
        "errors": [],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=MagicMock(),
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )
    service.config.screening_market_guard_enabled = False

    result = service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=None,
        candidate_limit=30,
        ai_top_k=0,
    )

    assert result["status"] == "failed"
    db.get_kline_audit_trade_date.assert_called_once_with(
        market="cn",
        trade_date=date(2026, 3, 13),
    )
    factor_service.build_factor_snapshot.assert_not_called()
    failed_call = db.update_screening_run_status.call_args_list[-1]
    assert "缺少 K 线审计记录" in failed_call.kwargs["error_summary"]


def test_screening_task_service_rerun_failed_skips_known_failed_symbols_before_sync():
    db = MagicMock()
    db.find_latest_screening_run.return_value = {
        "run_id": "run-known-bad",
        "mode": "balanced",
        "status": "failed",
        "candidate_count": 0,
        "config_snapshot": {
            "stock_codes": [],
            "failed_symbols": ["000002", "000003"],
            "failed_symbol_reasons": {"000002": "empty_data", "000003": "not_found"},
            "warnings": ["已跳过同步失败股票: 000002, 000003"],
            "next_resume_stage": "ingesting",
        },
    }
    db.get_screening_run.return_value = {
        "run_id": "run-known-bad",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 1,
        "config_snapshot": {
            "failed_symbols": ["000002", "000003"],
            "warnings": ["补跑跳过已确认无数据股票: 000002, 000003"],
            "sync_failure_ratio": 0.0,
        },
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [
            {"code": "600519", "name": "贵州茅台", "listing_status": "active"},
            {"code": "000002", "name": "未知股票A", "listing_status": "active"},
            {"code": "000003", "name": "未知股票B", "listing_status": "active"},
        ]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_service.build_factor_snapshot.return_value = pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])

    screener_service = MagicMock()
    screener_service.evaluate.return_value.selected = []
    screener_service.evaluate.return_value.rejected = []

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 1,
        "synced": 1,
        "skipped": 0,
        "errors": [],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=screener_service,
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )
    service.config.screening_ingest_failure_threshold = 0.4

    result = service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=None,
        rerun_failed=True,
        candidate_limit=30,
        ai_top_k=0,
    )

    assert result["status"] == "completed"
    sync_codes = market_data_sync_service.sync_trade_date.call_args.kwargs["stock_codes"]
    assert sync_codes == ["600519"]
    factor_universe = factor_service.build_factor_snapshot.call_args.kwargs["universe_df"]
    assert factor_universe["code"].tolist() == ["600519"]


def test_screening_task_service_factorizing_rerun_keeps_filtered_universe_and_warning_summary():
    db = MagicMock()
    db.find_latest_screening_run.return_value = {
        "run_id": "run-factorizing-known-bad",
        "mode": "balanced",
        "status": "failed",
        "candidate_count": 0,
        "config_snapshot": {
            "stock_codes": [],
            "failed_symbols": ["000002", "000003"],
            "failed_symbol_reasons": {"000002": "empty_data", "000003": "fetch_failed"},
            "warnings": ["已跳过同步失败股票: 000002, 000003"],
            "next_resume_stage": "factorizing",
        },
    }
    db.get_screening_run.return_value = {
        "run_id": "run-factorizing-known-bad",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 0,
        "config_snapshot": {
            "failed_symbols": ["000002", "000003"],
            "warnings": ["已跳过同步失败股票: 000002, 000003"],
            "sync_failure_ratio": 0.25,
        },
        "error_summary": "已跳过同步失败股票: 000002, 000003",
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [
            {"code": "600519", "name": "贵州茅台", "listing_status": "active"},
            {"code": "000002", "name": "未知股票A", "listing_status": "active"},
            {"code": "000003", "name": "未知股票B", "listing_status": "active"},
        ]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_service.build_factor_snapshot.return_value = pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])

    screener_service = MagicMock()
    screener_service.evaluate.return_value.selected = []
    screener_service.evaluate.return_value.rejected = []

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=screener_service,
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=MagicMock(),
    )

    result = service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=None,
        rerun_failed=True,
        resume_from="factorizing",
        candidate_limit=30,
        ai_top_k=0,
    )

    assert result["status"] == "completed"
    factor_universe = factor_service.build_factor_snapshot.call_args.kwargs["universe_df"]
    assert factor_universe["code"].tolist() == ["600519"]
    completed_call = db.update_screening_run_status.call_args_list[-1]
    assert "已跳过同步失败股票: 000002, 000003" in completed_call.kwargs["error_summary"]
    assert "补跑跳过已确认无数据股票: 000002, 000003" in completed_call.kwargs["error_summary"]


def test_screening_task_service_rerun_ingesting_clears_stale_fetch_failed_marker_after_success():
    db = MagicMock()
    db.find_latest_screening_run.return_value = {
        "run_id": "run-rerun-fetch-failed",
        "mode": "balanced",
        "status": "failed",
        "candidate_count": 0,
        "config_snapshot": {
            "stock_codes": [],
            "failed_symbols": ["000002"],
            "failed_symbol_reasons": {"000002": "fetch_failed"},
            "warnings": ["已跳过同步失败股票: 000002"],
            "next_resume_stage": "ingesting",
        },
    }
    db.get_screening_run.return_value = {
        "run_id": "run-rerun-fetch-failed",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 0,
        "config_snapshot": {
            "failed_symbols": [],
            "warnings": [],
            "sync_failure_ratio": 0.0,
        },
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [
            {"code": "600519", "name": "贵州茅台", "listing_status": "active"},
            {"code": "000002", "name": "未知股票A", "listing_status": "active"},
        ]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_service.build_factor_snapshot.return_value = pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])

    screener_service = MagicMock()
    screener_service.evaluate.return_value.selected = []
    screener_service.evaluate.return_value.rejected = []

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 2,
        "synced": 2,
        "skipped": 0,
        "errors": [],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=screener_service,
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )

    result = service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=None,
        rerun_failed=True,
        candidate_limit=30,
        ai_top_k=0,
    )

    assert result["status"] == "completed"
    sync_codes = market_data_sync_service.sync_trade_date.call_args.kwargs["stock_codes"]
    assert sync_codes == ["600519", "000002"]
    update_context_calls = db.update_screening_run_context.call_args_list
    assert any(call.kwargs["config_snapshot_updates"]["failed_symbols"] == [] for call in update_context_calls)


def test_screening_task_service_does_not_fail_when_sync_failure_ratio_equals_threshold():
    db = MagicMock()
    db.create_screening_run.return_value = "run-006-threshold-equal"
    db.get_screening_run.return_value = {
        "run_id": "run-006-threshold-equal",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 1,
        "config_snapshot": {
            "failed_symbols": ["000002"],
            "warnings": ["已跳过同步失败股票: 000002"],
            "sync_failure_ratio": 0.3333,
        },
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [
            {"code": "600519", "name": "贵州茅台", "listing_status": "active"},
            {"code": "000001", "name": "平安银行", "listing_status": "active"},
            {"code": "000002", "name": "未知股票", "listing_status": "active"},
        ]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_service.build_factor_snapshot.return_value = pd.DataFrame([{"code": "600519", "name": "贵州茅台"}])

    screener_service = MagicMock()
    screener_service.evaluate.return_value.selected = []
    screener_service.evaluate.return_value.rejected = []

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 3,
        "synced": 2,
        "skipped": 0,
        "errors": [{"code": "000002", "reason": "empty_data", "detail": "no data"}],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=screener_service,
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )
    service.config.screening_ingest_failure_threshold = 1 / 3

    result = service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=None,
        candidate_limit=30,
        ai_top_k=0,
    )

    assert result["status"] == "completed"
    factor_service.build_factor_snapshot.assert_called_once()


def test_screening_task_service_does_not_ignore_non_delisted_inactive_status_sync_errors():
    db = MagicMock()
    db.create_screening_run.return_value = "run-006c"
    db.get_screening_run.return_value = {
        "run_id": "run-006c",
        "mode": "balanced",
        "status": "failed",
        "candidate_count": 0,
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [
            {"code": "600519", "name": "贵州茅台", "listing_status": "active"},
            {"code": "000001", "name": "平安银行", "listing_status": "suspended"},
        ]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 2,
        "synced": 1,
        "skipped": 0,
        "errors": [{"code": "000001", "reason": "empty_data"}],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=MagicMock(),
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )

    result = service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=None,
        candidate_limit=30,
        ai_top_k=0,
    )

    assert result["status"] == "failed"
    factor_service.build_factor_snapshot.assert_not_called()
    failed_call = db.update_screening_run_status.call_args_list[-1]
    assert "000001" in failed_call.kwargs["error_summary"]


def test_screening_task_service_fails_when_ignorable_and_blocking_sync_errors_coexist():
    db = MagicMock()
    db.create_screening_run.return_value = "run-006d"
    db.get_screening_run.return_value = {
        "run_id": "run-006d",
        "mode": "balanced",
        "status": "failed",
        "candidate_count": 0,
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [
            {"code": "600519", "name": "贵州茅台", "listing_status": "active"},
            {"code": "000002", "name": "万科A退", "listing_status": "active"},
            {"code": "000001", "name": "平安银行", "listing_status": "active"},
        ]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 3,
        "synced": 1,
        "skipped": 0,
        "errors": [
            {"code": "000002", "reason": "empty_data"},
            {"code": "000001", "reason": "save_failed"},
        ],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=MagicMock(),
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )

    result = service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=None,
        candidate_limit=30,
        ai_top_k=0,
    )

    assert result["status"] == "failed"
    factor_service.build_factor_snapshot.assert_not_called()
    failed_call = db.update_screening_run_status.call_args_list[-1]
    assert "000001" in failed_call.kwargs["error_summary"]
    assert "save_failed" in failed_call.kwargs["error_summary"]


def test_screening_task_service_does_not_ignore_regular_name_containing_tui_character():
    db = MagicMock()
    db.create_screening_run.return_value = "run-006e"
    db.get_screening_run.return_value = {
        "run_id": "run-006e",
        "mode": "balanced",
        "status": "failed",
        "candidate_count": 0,
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [
            {"code": "600519", "name": "贵州茅台", "listing_status": "active"},
            {"code": "000003", "name": "进退科技", "listing_status": "active"},
        ]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 2,
        "synced": 1,
        "skipped": 0,
        "errors": [{"code": "000003", "reason": "empty_data"}],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=MagicMock(),
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )

    result = service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=None,
        candidate_limit=30,
        ai_top_k=0,
    )

    assert result["status"] == "failed"
    factor_service.build_factor_snapshot.assert_not_called()
    failed_call = db.update_screening_run_status.call_args_list[-1]
    assert "000003" in failed_call.kwargs["error_summary"]


def test_screening_task_service_reports_filtered_empty_universe_after_ignoring_delisted_errors():
    db = MagicMock()
    db.create_screening_run.return_value = "run-006f"
    db.get_screening_run.return_value = {
        "run_id": "run-006f",
        "mode": "balanced",
        "status": "failed",
        "candidate_count": 0,
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [{"code": "000002", "name": "万科A退", "listing_status": "active"}]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 1,
        "synced": 0,
        "skipped": 0,
        "errors": [
            {
                "code": "000002",
                "reason": "empty_data",
                "detail": "所有数据源获取 000002 失败: no data from providers",
            }
        ],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=MagicMock(),
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )

    result = service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=None,
        candidate_limit=30,
        ai_top_k=0,
    )

    assert result["status"] == "failed"
    factor_service.build_factor_snapshot.assert_not_called()
    failed_call = db.update_screening_run_status.call_args_list[-1]
    assert "剔除同步失败股票后" in failed_call.kwargs["error_summary"]


def test_screening_task_service_persists_failed_run_to_real_database():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = os.path.join(temp_dir, "screening_task_failed.db")
        os.environ["DATABASE_PATH"] = db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()

        try:
            db = DatabaseManager.get_instance()
            service = ScreeningTaskService(
                db_manager=db,
                universe_service=_IntegrationUniverseService(),
                factor_service=MagicMock(),
                screener_service=MagicMock(),
                candidate_analysis_service=MagicMock(),
                market_data_sync_service=MagicMock(),
            )

            result = service.execute_run(
                trade_date=date(2026, 3, 13),
                stock_codes=None,
                candidate_limit=30,
                ai_top_k=0,
            )

            persisted = db.get_screening_run(result["run_id"])
            assert result["status"] == "failed"
            assert persisted is not None
            assert persisted["status"] == "failed"
            assert persisted["error_summary"] == "sync failed"
            assert persisted["completed_at"] is not None
        finally:
            DatabaseManager.reset_instance()
            Config.reset_instance()
            os.environ.pop("DATABASE_PATH", None)


def test_screening_task_service_persists_effective_trade_date_on_failed_run():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = os.path.join(temp_dir, "screening_task_failed_trade_date.db")
        os.environ["DATABASE_PATH"] = db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()

        try:
            db = DatabaseManager.get_instance()
            db.create_kline_audit_run(
                run_id="audit-run-failed-trade-date",
                market="cn",
                trade_date=date(2026, 3, 12),
                run_type="daily",
                trigger_type="manual",
                run_result="succeeded",
                pass_status="passed",
                rule_version="test-v1",
                window_start=date(2025, 12, 20),
                window_end=date(2026, 3, 12),
            )
            db.upsert_kline_audit_trade_date(
                market="cn",
                trade_date=date(2026, 3, 12),
                pass_status="passed",
                window_start=date(2025, 12, 20),
                window_end=date(2026, 3, 12),
                rule_version="test-v1",
                source_run_id="audit-run-failed-trade-date",
                passed_at=None,
            )
            factor_service = MagicMock()
            factor_service.get_latest_trade_date.return_value = date(2026, 3, 12)
            factor_service.build_factor_snapshot.side_effect = ValueError("factor build failed")

            market_data_sync_service = MagicMock()
            market_data_sync_service.sync_trade_date.return_value = {
                "trade_date": "2026-03-12",
                "total": 1,
                "synced": 1,
                "skipped": 0,
                "errors": [],
            }

            service = ScreeningTaskService(
                db_manager=db,
                universe_service=_SuccessUniverseService(),
                factor_service=factor_service,
                screener_service=MagicMock(),
                candidate_analysis_service=MagicMock(),
                market_data_sync_service=market_data_sync_service,
            )

            result = service.execute_run(
                trade_date=None,
                stock_codes=None,
                candidate_limit=30,
                ai_top_k=0,
            )

            persisted = db.get_screening_run(result["run_id"])
            assert result["status"] == "failed"
            assert persisted is not None
            assert persisted["trade_date"] == "2026-03-12"
            assert persisted["error_summary"] == "factor build failed"
        finally:
            DatabaseManager.reset_instance()
            Config.reset_instance()
            os.environ.pop("DATABASE_PATH", None)


def test_screening_task_service_updates_trade_date_in_real_database():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = os.path.join(temp_dir, "screening_task_success.db")
        os.environ["DATABASE_PATH"] = db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()

        try:
            db = DatabaseManager.get_instance()
            db.create_kline_audit_run(
                run_id="audit-run-success-trade-date",
                market="cn",
                trade_date=date(2026, 3, 13),
                run_type="daily",
                trigger_type="manual",
                run_result="succeeded",
                pass_status="passed",
                rule_version="test-v1",
                window_start=date(2025, 12, 20),
                window_end=date(2026, 3, 13),
            )
            db.upsert_kline_audit_trade_date(
                market="cn",
                trade_date=date(2026, 3, 13),
                pass_status="passed",
                window_start=date(2025, 12, 20),
                window_end=date(2026, 3, 13),
                rule_version="test-v1",
                source_run_id="audit-run-success-trade-date",
                passed_at=None,
            )
            factor_service = MagicMock()
            factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)
            factor_service.build_factor_snapshot.return_value = pd.DataFrame(
                [{"code": "600519", "name": "贵州茅台", "close": 1500.0, "rule_score": 0}]
            )

            screener_service = MagicMock()
            screener_service.evaluate.return_value.selected = []
            screener_service.evaluate.return_value.rejected = []

            market_data_sync_service = MagicMock()
            market_data_sync_service.sync_trade_date.return_value = {
                "trade_date": "2026-03-13",
                "total": 1,
                "synced": 1,
                "skipped": 0,
                "errors": [],
            }

            service = ScreeningTaskService(
                db_manager=db,
                universe_service=_SuccessUniverseService(),
                factor_service=factor_service,
                screener_service=screener_service,
                candidate_analysis_service=MagicMock(),
                market_data_sync_service=market_data_sync_service,
            )

            result = service.execute_run(
                trade_date=None,
                stock_codes=None,
                candidate_limit=30,
                ai_top_k=0,
            )

            persisted = db.get_screening_run(result["run_id"])
            assert result["status"] == "completed"
            assert persisted is not None
            assert persisted["trade_date"] == "2026-03-13"
        finally:
            DatabaseManager.reset_instance()
            Config.reset_instance()
            os.environ.pop("DATABASE_PATH", None)


# ---------------------------------------------------------------------------
# Phase: 策略路由 + 大盘 MA100 门控
# ---------------------------------------------------------------------------


@patch("src.services.screening_task_service.ScreenerService")
@patch("src.services.screening_task_service.FactorService")
@patch("src.services.screening_task_service.get_config")
def test_execute_run_passes_strategy_names_to_screener_service(
    get_config_mock,
    factor_cls,
    screener_cls,
):
    """strategy_names 应从 execute_run 传递到 ScreenerService 构造函数。"""
    get_config_mock.return_value.screening_default_mode = "balanced"
    get_config_mock.return_value.screening_candidate_limit = 30
    get_config_mock.return_value.screening_ai_top_k = 5
    get_config_mock.return_value.screening_min_list_days = 120
    get_config_mock.return_value.screening_min_volume_ratio = 1.2
    get_config_mock.return_value.screening_breakout_lookback_days = 20
    get_config_mock.return_value.screening_factor_lookback_days = 80
    get_config_mock.return_value.screening_market_guard_enabled = False
    get_config_mock.return_value.screening_ingest_failure_threshold = 0.20

    db = MagicMock()
    db.create_screening_run.return_value = "run-strat-001"
    db.get_screening_run.return_value = {
        "run_id": "run-strat-001",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 0,
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台"}]
    )

    screener_instance = MagicMock()
    screener_instance.evaluate.return_value.selected = []
    screener_instance.evaluate.return_value.rejected = []
    screener_cls.return_value = screener_instance

    factor_instance = MagicMock()
    factor_instance.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_instance.build_factor_snapshot.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台"}]
    )
    factor_cls.return_value = factor_instance

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 1,
        "synced": 1,
        "skipped": 0,
        "errors": [],
    }

    skill_manager = MagicMock()
    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=None,
        screener_service=None,
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
        skill_manager=skill_manager,
    )

    service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=None,
        strategy_names=["pattern_123_bottom"],
    )

    screener_cls.assert_called_once_with(
        min_list_days=120,
        min_volume_ratio=1.2,
        breakout_lookback_days=20,
        skill_manager=skill_manager,
        strategy_names=["pattern_123_bottom"],
    )


@patch("src.services.screening_task_service.ScreenerService")
@patch("src.services.screening_task_service.FactorService")
@patch("src.services.screening_task_service.get_config")
def test_execute_run_without_strategies_passes_none(
    get_config_mock,
    factor_cls,
    screener_cls,
):
    """不指定 strategy_names 时，ScreenerService 应收到 None。"""
    get_config_mock.return_value.screening_default_mode = "balanced"
    get_config_mock.return_value.screening_candidate_limit = 30
    get_config_mock.return_value.screening_ai_top_k = 5
    get_config_mock.return_value.screening_min_list_days = 120
    get_config_mock.return_value.screening_min_volume_ratio = 1.2
    get_config_mock.return_value.screening_breakout_lookback_days = 20
    get_config_mock.return_value.screening_factor_lookback_days = 80
    get_config_mock.return_value.screening_market_guard_enabled = False
    get_config_mock.return_value.screening_ingest_failure_threshold = 0.20

    db = MagicMock()
    db.create_screening_run.return_value = "run-strat-002"
    db.get_screening_run.return_value = {
        "run_id": "run-strat-002",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 0,
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台"}]
    )

    screener_instance = MagicMock()
    screener_instance.evaluate.return_value.selected = []
    screener_instance.evaluate.return_value.rejected = []
    screener_cls.return_value = screener_instance

    factor_instance = MagicMock()
    factor_instance.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_instance.build_factor_snapshot.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台"}]
    )
    factor_cls.return_value = factor_instance

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 1,
        "synced": 1,
        "skipped": 0,
        "errors": [],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=None,
        screener_service=None,
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
        skill_manager=None,
    )

    service.execute_run(
        trade_date=date(2026, 3, 13),
        stock_codes=None,
    )

    screener_cls.assert_called_once_with(
        min_list_days=120,
        min_volume_ratio=1.2,
        breakout_lookback_days=20,
        skill_manager=None,
        strategy_names=None,
    )


def test_execute_run_adds_warning_when_market_guard_unsafe():
    """大盘低于 MA100 时，选股照常执行但 warnings 中包含大盘情绪提示。"""
    db = MagicMock()
    db.create_screening_run.return_value = "run-guard-001"
    db.get_screening_run.return_value = {
        "run_id": "run-guard-001",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 0,
        "config_snapshot": {
            "warnings": [
                "⚠️ 大盘情绪低迷，不建议操作 — 上证指数 3100.00 低于 MA100 (3200.00)，跌幅 3.1%"
            ],
        },
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台"}]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_service.build_factor_snapshot.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台"}]
    )

    screener_service = MagicMock()
    screener_service.evaluate.return_value.selected = []
    screener_service.evaluate.return_value.rejected = []

    market_data_sync_service = MagicMock()
    market_data_sync_service.fetcher_manager = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 1,
        "synced": 1,
        "skipped": 0,
        "errors": [],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=screener_service,
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )
    service.config.screening_market_guard_enabled = True
    service.config.screening_market_guard_index = "sh000001"

    with patch("src.services.screening_task_service.MarketGuard") as guard_cls:
        guard_instance = MagicMock()
        guard_instance.check.return_value = MagicMock(
            is_safe=False,
            index_price=3100.0,
            index_ma100=3200.0,
            message="Index sh000001 below MA100 (3100.00 < 3200.00, -3.1%)",
        )
        guard_cls.return_value = guard_instance

        result = service.execute_run(
            trade_date=date(2026, 3, 13),
            stock_codes=None,
        )

    # 选股正常完成，不被中断
    assert result["status"] == "completed"


def test_execute_run_keeps_candidate_limit_under_defensive_regime():
    """defensive 环境只加风险提示，不应缩减用户请求的 candidate_limit。"""
    from src.schemas.trading_types import MarketRegime, RiskLevel

    db = MagicMock()
    db.create_screening_run.return_value = "run-defensive-001"
    db.get_screening_run.return_value = {
        "run_id": "run-defensive-001",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 0,
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台"}]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_service.build_factor_snapshot.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台"}]
    )

    market_data_sync_service = MagicMock()
    market_data_sync_service.fetcher_manager = MagicMock()
    market_data_sync_service.fetcher_manager.get_market_stats.return_value = {
        "limit_up_count": 20,
        "limit_down_count": 15,
        "up_count": 1800,
        "down_count": 2200,
    }
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 1,
        "synced": 1,
        "skipped": 0,
        "errors": [],
    }

    pipeline_candidates = [
        SimpleNamespace(
            code=f"60051{i}",
            name=f"候选{i}",
            rank=i + 1,
            rule_score=90.0 - i,
            matched_strategies=["volume_breakout"],
            strategy_scores={"volume_breakout": 90.0 - i},
            rule_hits=["trend_aligned"],
            factor_snapshot={},
            setup_type="bottom_divergence_breakout",
            entry_maturity="high",
            trade_stage="focus",
            market_regime="defensive",
            risk_level="high",
            theme_position="secondary_theme",
            candidate_pool_level="focus_list",
        )
        for i in range(5)
    ]

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=MagicMock(),
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )
    service.config.screening_market_guard_enabled = True
    service.config.screening_market_guard_index = "sh000001"

    with patch("src.services.screening_task_service.MarketGuard") as guard_cls, patch(
        "src.services.market_environment_engine.MarketEnvironmentEngine.assess"
    ) as assess_mock, patch("src.services.five_layer_pipeline.FiveLayerPipeline") as pipeline_cls:
        guard_instance = MagicMock()
        guard_instance.check.return_value = MagicMock(
            is_safe=False,
            index_price=3100.0,
            index_ma100=3200.0,
            message="Index sh000001 below MA100 (3100.00 < 3200.00, -3.1%)",
        )
        guard_instance.get_index_bars.return_value = []
        guard_cls.return_value = guard_instance

        assess_mock.return_value = SimpleNamespace(
            regime=MarketRegime.DEFENSIVE,
            risk_level=RiskLevel.HIGH,
            is_safe=False,
            message="defensive market",
        )
        pipeline_cls.return_value.run.return_value = SimpleNamespace(
            candidates=pipeline_candidates,
            decision_context={"pipeline_stats": {"selected_after_limit": 5}},
            pipeline_stats={"selected_after_limit": 5, "matched_before_limit": 5, "rejected_before_l345": 0},
        )

        result = service.execute_run(
            trade_date=date(2026, 3, 13),
            stock_codes=None,
            candidate_limit=5,
            ai_top_k=0,
        )

    assert result["status"] == "completed"
    assert pipeline_cls.return_value.run.call_args.kwargs["candidate_limit"] == 5
    saved_candidates = db.save_screening_candidates.call_args.kwargs["candidates"]
    assert len(saved_candidates) == 5
    # MarketGuard 被正确构造
    guard_cls.assert_called_once_with(
        fetcher_manager=market_data_sync_service.fetcher_manager,
        index_code="sh000001",
    )


def test_execute_run_proceeds_when_market_guard_safe():
    """大盘在 MA100 之上时，execute_run 应正常执行。"""
    db = MagicMock()
    db.create_screening_run.return_value = "run-guard-002"
    db.get_screening_run.return_value = {
        "run_id": "run-guard-002",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 1,
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台"}]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_service.build_factor_snapshot.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台"}]
    )

    screener_service = MagicMock()
    screener_service.evaluate.return_value.selected = []
    screener_service.evaluate.return_value.rejected = []

    candidate_analysis_service = MagicMock()
    candidate_analysis_service.analyze_top_k.return_value = {}

    market_data_sync_service = MagicMock()
    market_data_sync_service.fetcher_manager = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 1,
        "synced": 1,
        "skipped": 0,
        "errors": [],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=screener_service,
        candidate_analysis_service=candidate_analysis_service,
        market_data_sync_service=market_data_sync_service,
    )
    service.config.screening_market_guard_enabled = True
    service.config.screening_market_guard_index = "sh000001"

    with patch("src.services.screening_task_service.MarketGuard") as guard_cls:
        guard_instance = MagicMock()
        guard_instance.check.return_value = MagicMock(
            is_safe=True,
            index_price=3300.0,
            index_ma100=3200.0,
            message="Index sh000001 above MA100 (3300.00 > 3200.00, +3.1%)",
        )
        guard_cls.return_value = guard_instance

        result = service.execute_run(
            trade_date=date(2026, 3, 13),
            stock_codes=None,
        )

    assert result["status"] == "completed"


def test_execute_run_skips_guard_when_disabled():
    """screening_market_guard_enabled=False 时不调用 MarketGuard。"""
    db = MagicMock()
    db.create_screening_run.return_value = "run-guard-003"
    db.get_screening_run.return_value = {
        "run_id": "run-guard-003",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 0,
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台"}]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_service.build_factor_snapshot.return_value = pd.DataFrame(
        [{"code": "600519", "name": "贵州茅台"}]
    )

    screener_service = MagicMock()
    screener_service.evaluate.return_value.selected = []
    screener_service.evaluate.return_value.rejected = []

    market_data_sync_service = MagicMock()
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": 1,
        "synced": 1,
        "skipped": 0,
        "errors": [],
    }

    service = ScreeningTaskService(
        db_manager=db,
        universe_service=universe_service,
        factor_service=factor_service,
        screener_service=screener_service,
        candidate_analysis_service=MagicMock(),
        market_data_sync_service=market_data_sync_service,
    )
    service.config.screening_market_guard_enabled = False

    with patch("src.services.screening_task_service.MarketGuard") as guard_cls:
        result = service.execute_run(
            trade_date=date(2026, 3, 13),
            stock_codes=None,
        )

    guard_cls.assert_not_called()
    assert result["status"] == "completed"


def test_strategy_names_written_to_run_config_snapshot():
    """strategy_names 应写入 run 的 config_snapshot 以便追溯。"""
    snapshot = ScreeningTaskService._build_run_config_snapshot(
        requested_trade_date=date(2026, 3, 13),
        normalized_stock_codes=[],
        runtime_config=MagicMock(to_snapshot=lambda: {"mode": "balanced"}),
        ingest_failure_threshold=0.20,
        strategy_names=["pattern_123_bottom", "ma100_selection"],
    )
    assert snapshot["strategy_names"] == ["ma100_selection", "pattern_123_bottom"]


def test_strategy_names_omitted_from_snapshot_when_none():
    """strategy_names=None 时，config_snapshot 中不应包含该字段。"""
    snapshot = ScreeningTaskService._build_run_config_snapshot(
        requested_trade_date=date(2026, 3, 13),
        normalized_stock_codes=[],
        runtime_config=MagicMock(to_snapshot=lambda: {"mode": "balanced"}),
        ingest_failure_threshold=0.20,
        strategy_names=None,
    )
    assert "strategy_names" not in snapshot
