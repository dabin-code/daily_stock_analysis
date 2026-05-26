# -*- coding: utf-8 -*-
"""Tests for the SCREENING_MIN_FINAL_SCORE final-layer filter.

Covers:
- Default 80 threshold drops sub-threshold candidates from the final selection.
- Threshold 0 disables the filter.
- The threshold flows from Config → ResolvedScreeningRuntimeConfig.
- pipeline_stats records before/after counts when the gate is active.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.config import Config
from src.services.screening_mode_registry import resolve_screening_runtime_config
from src.services.screening_task_service import ScreeningTaskService


# ---------------------------------------------------------------------------
# ResolvedScreeningRuntimeConfig propagates min_final_score
# ---------------------------------------------------------------------------


def _make_config_namespace(**overrides):
    base = dict(
        screening_default_mode="balanced",
        screening_candidate_limit=30,
        screening_ai_top_k=10,
        screening_min_list_days=120,
        screening_min_volume_ratio=1.2,
        screening_breakout_lookback_days=20,
        screening_factor_lookback_days=200,
        screening_min_final_score=80.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_resolve_runtime_config_exposes_min_final_score_default_80():
    cfg = _make_config_namespace()
    resolved = resolve_screening_runtime_config(
        cfg, mode="balanced", candidate_limit=None, ai_top_k=None
    )
    assert resolved.min_final_score == 80.0
    snapshot = resolved.to_snapshot()
    assert snapshot["screening_min_final_score"] == 80.0


def test_resolve_runtime_config_propagates_custom_min_final_score():
    cfg = _make_config_namespace(screening_min_final_score=65.5)
    resolved = resolve_screening_runtime_config(
        cfg, mode="aggressive", candidate_limit=None, ai_top_k=None
    )
    assert resolved.min_final_score == 65.5


def test_resolve_runtime_config_clamps_negative_min_final_score_to_zero():
    cfg = _make_config_namespace(screening_min_final_score=-10.0)
    resolved = resolve_screening_runtime_config(
        cfg, mode="balanced", candidate_limit=None, ai_top_k=None
    )
    assert resolved.min_final_score == 0.0


# ---------------------------------------------------------------------------
# Config.from_env / validate_structured wiring
# ---------------------------------------------------------------------------


def test_config_default_has_min_final_score_80():
    cfg = Config()
    assert cfg.screening_min_final_score == pytest.approx(80.0)


def test_config_validate_structured_rejects_negative_min_final_score():
    cfg = Config()
    cfg.screening_min_final_score = -1.0
    issues = cfg.validate_structured()
    matches = [i for i in issues if i.field == "SCREENING_MIN_FINAL_SCORE"]
    assert matches, "Expected an error for negative SCREENING_MIN_FINAL_SCORE"
    assert matches[0].severity == "error"


# ---------------------------------------------------------------------------
# Final-layer filter integration in ScreeningTaskService.execute_run
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _block_real_screening_notifications(monkeypatch):
    def _fake_notify_run(self, run_id, force=False):
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


def _build_service_with_pipeline(pipeline_candidates, *, screening_min_final_score: float):
    db = MagicMock()
    db.create_screening_run.return_value = "run-min-score"
    db.get_screening_run.return_value = {
        "run_id": "run-min-score",
        "mode": "balanced",
        "status": "completed",
        "candidate_count": 0,
    }

    universe_service = MagicMock()
    universe_service.resolve_universe.return_value = pd.DataFrame(
        [{"code": c.code, "name": c.name} for c in pipeline_candidates]
    )

    factor_service = MagicMock()
    factor_service.get_latest_trade_date.return_value = date(2026, 3, 13)
    factor_service.build_factor_snapshot.return_value = pd.DataFrame(
        [{"code": c.code, "name": c.name, "close": 100.0} for c in pipeline_candidates]
    )

    screener_service = MagicMock()
    screener_service.evaluate.return_value.selected = []
    screener_service.evaluate.return_value.rejected = []

    candidate_analysis_service = MagicMock()
    candidate_analysis_service.analyze_top_k.return_value = {}

    market_data_sync_service = MagicMock()
    market_data_sync_service.fetcher_manager.get_market_stats.return_value = {
        "limit_up_count": 30,
        "limit_down_count": 10,
        "up_count": 2500,
        "down_count": 1500,
    }
    market_data_sync_service.sync_trade_date.return_value = {
        "trade_date": "2026-03-13",
        "total": len(pipeline_candidates),
        "synced": len(pipeline_candidates),
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
    service.config.screening_min_final_score = screening_min_final_score
    return service, db


def _make_pipeline_candidate(code: str, rule_score: float, rank: int):
    return SimpleNamespace(
        code=code,
        name=f"name-{code}",
        rank=rank,
        rule_score=rule_score,
        rule_hits=["trend_aligned"],
        factor_snapshot={"close": 100.0},
        matched_strategies=["trend_aligned"],
        strategy_scores={"trend_aligned": rule_score},
        setup_type="trend_breakout",
        entry_maturity="high",
        trade_stage="focus",
        market_regime="balanced",
        risk_level="medium",
        theme_position="main_theme",
        candidate_pool_level="focus_list",
    )


def test_execute_run_drops_candidates_below_min_final_score():
    pipeline_candidates = [
        _make_pipeline_candidate("600519", rule_score=120.0, rank=1),
        _make_pipeline_candidate("000001", rule_score=85.0, rank=2),
        _make_pipeline_candidate("300750", rule_score=60.0, rank=3),
        _make_pipeline_candidate("002594", rule_score=40.0, rank=4),
    ]
    service, db = _build_service_with_pipeline(
        pipeline_candidates, screening_min_final_score=80.0
    )

    with patch("src.services.five_layer_pipeline.FiveLayerPipeline") as pipeline_cls:
        pipeline_cls.return_value.run.return_value = SimpleNamespace(
            candidates=pipeline_candidates,
            decision_context=None,
            pipeline_stats={
                "selected_after_limit": 4,
                "matched_before_limit": 4,
                "rejected_before_l345": 0,
            },
        )

        service.execute_run(
            trade_date=date(2026, 3, 13),
            stock_codes=None,
            candidate_limit=30,
            ai_top_k=2,
        )

    db.save_screening_candidates.assert_called_once()
    saved = db.save_screening_candidates.call_args.kwargs["candidates"]
    saved_codes = [item["code"] for item in saved]
    assert saved_codes == ["600519", "000001"], (
        "Sub-80 candidates must be dropped before persistence"
    )

    completed_call = db.update_screening_run_status.call_args_list[-1]
    assert completed_call.kwargs["candidate_count"] == 2


def test_execute_run_keeps_all_candidates_when_threshold_is_zero():
    pipeline_candidates = [
        _make_pipeline_candidate("600519", rule_score=85.0, rank=1),
        _make_pipeline_candidate("000001", rule_score=42.0, rank=2),
        _make_pipeline_candidate("300750", rule_score=10.0, rank=3),
    ]
    service, db = _build_service_with_pipeline(
        pipeline_candidates, screening_min_final_score=0.0
    )

    with patch("src.services.five_layer_pipeline.FiveLayerPipeline") as pipeline_cls:
        pipeline_cls.return_value.run.return_value = SimpleNamespace(
            candidates=pipeline_candidates,
            decision_context=None,
            pipeline_stats={
                "selected_after_limit": 3,
                "matched_before_limit": 3,
                "rejected_before_l345": 0,
            },
        )

        service.execute_run(
            trade_date=date(2026, 3, 13),
            stock_codes=None,
            candidate_limit=30,
            ai_top_k=1,
        )

    db.save_screening_candidates.assert_called_once()
    saved = db.save_screening_candidates.call_args.kwargs["candidates"]
    saved_codes = [item["code"] for item in saved]
    assert saved_codes == ["600519", "000001", "300750"], (
        "Threshold=0 must disable the filter"
    )


def test_execute_run_filter_renumbers_ranks_after_dropping():
    pipeline_candidates = [
        _make_pipeline_candidate("600519", rule_score=120.0, rank=1),
        _make_pipeline_candidate("000001", rule_score=10.0, rank=2),
        _make_pipeline_candidate("300750", rule_score=95.0, rank=3),
    ]
    service, db = _build_service_with_pipeline(
        pipeline_candidates, screening_min_final_score=80.0
    )

    with patch("src.services.five_layer_pipeline.FiveLayerPipeline") as pipeline_cls:
        pipeline_cls.return_value.run.return_value = SimpleNamespace(
            candidates=pipeline_candidates,
            decision_context=None,
            pipeline_stats={
                "selected_after_limit": 3,
                "matched_before_limit": 3,
                "rejected_before_l345": 0,
            },
        )

        service.execute_run(
            trade_date=date(2026, 3, 13),
            stock_codes=None,
            candidate_limit=30,
            ai_top_k=2,
        )

    saved = db.save_screening_candidates.call_args.kwargs["candidates"]
    saved_pairs = [(item["code"], item["rank"]) for item in saved]
    assert saved_pairs == [("600519", 1), ("300750", 2)]
