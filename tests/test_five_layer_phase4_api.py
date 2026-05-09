# -*- coding: utf-8 -*-
"""Tests for Phase 4: API endpoints and agent tools for five-layer backtest."""
from __future__ import annotations

import json
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v1.endpoints.five_layer_backtest import router
from api.v1.schemas.five_layer_backtest import (
    FiveLayerBacktestRunRequest,
    FiveLayerCalibrationRequest,
    FiveLayerEvaluationItem,
    FiveLayerGroupSummaryItem,
    FiveLayerRunResponse,
    FiveLayerEvaluationsResponse,
    FiveLayerSummariesResponse,
    FiveLayerCalibrationResponse,
    FiveLayerRecommendationsResponse,
    FiveLayerFullPipelineResponse,
)
from src.backtest.aggregators.ranking_effectiveness import (
    RankingComparisonResult,
    RankingEffectivenessReport,
)


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def app():
    _app = FastAPI()
    _app.include_router(router, prefix="/five-layer-backtest")
    return _app


@pytest.fixture
def client(app):
    return TestClient(app)


def _mock_run(run_id="flbt-test123456"):
    run = MagicMock()
    run.backtest_run_id = run_id
    run.evaluation_mode = "historical_snapshot"
    run.execution_model = "conservative"
    run.trade_date_from = date(2026, 3, 1)
    run.trade_date_to = date(2026, 3, 31)
    run.market = "cn"
    run.status = "completed"
    run.sample_count = 50
    run.completed_count = 48
    run.error_count = 2
    run.data_version = None
    run.market_data_version = None
    run.theme_mapping_version = None
    run.candidate_snapshot_version = None
    run.rules_version = None
    run.created_at = datetime(2026, 4, 1, 10, 0, 0)
    run.started_at = datetime(2026, 4, 1, 10, 0, 1)
    run.completed_at = datetime(2026, 4, 1, 10, 5, 0)
    run.to_dict = MagicMock(return_value={
        "id": 1,
        "backtest_run_id": run_id,
        "evaluation_mode": "historical_snapshot",
        "execution_model": "conservative",
        "trade_date_from": "2026-03-01",
        "trade_date_to": "2026-03-31",
        "market": "cn",
        "status": "completed",
        "sample_count": 50,
        "completed_count": 48,
        "error_count": 2,
        "data_version": None,
        "market_data_version": None,
        "theme_mapping_version": None,
        "candidate_snapshot_version": None,
        "rules_version": None,
        "config_json": json.dumps({
            "sample_baseline": {
                "raw_sample_count": 50,
                "evaluated_sample_count": 48,
                "aggregatable_sample_count": 30,
                "entry_sample_count": 30,
                "observation_sample_count": 18,
                "suppressed_sample_count": 18,
                "suppressed_reasons": {
                    "missing_forward_return_5d": 12,
                    "missing_risk_avoided_pct": 6,
                },
            },
        }),
        "created_at": "2026-04-01T10:00:00",
        "started_at": "2026-04-01T10:00:01",
        "completed_at": "2026-04-01T10:05:00",
    })
    return run


def _mock_evaluation(code="600519"):
    e = MagicMock()
    e.code = code
    e.to_dict = MagicMock(return_value={
        "id": 1,
        "backtest_run_id": "flbt-test123456",
        "trade_date": "2026-03-15",
        "code": code,
        "name": "贵州茅台",
        "signal_family": "entry",
        "evaluator_type": "entry",
        "execution_model": "conservative",
        "entry_fill_status": "filled",
        "entry_fill_price": 1800.0,
        "forward_return_5d": 2.5,
        "metrics_json": json.dumps({
            "sample_bucket": "core",
            "entry_timing_label": "on_time",
        }),
        "outcome": "win",
        "eval_status": "evaluated",
        "evidence_json": json.dumps({
            "matched_strategies": ["trend_breakout"],
            "primary_strategy": "trend_breakout",
            "contributing_strategies": ["volume_breakout"],
        }),
        "factor_snapshot_json": json.dumps({
            "ma100_low123_validation_status": "confirmed_missing_breakout_bar_index",
            "ma100_low123_data_complete": False,
        }),
    })
    return e


def _mock_evaluation_with_invalid_strategy_lists(code="600519"):
    e = MagicMock()
    e.code = code
    e.to_dict = MagicMock(return_value={
        "id": 1,
        "backtest_run_id": "flbt-test123456",
        "trade_date": "2026-03-15",
        "code": code,
        "name": "贵州茅台",
        "signal_family": "entry",
        "evaluator_type": "entry",
        "execution_model": "conservative",
        "evidence_json": json.dumps({
            "matched_strategies": [{"bad": "shape"}],
            "contributing_strategies": [1, None],
        }),
        "metrics_json": json.dumps({}),
        "factor_snapshot_json": json.dumps({}),
    })
    return e


def _mock_summary(group_type="overall", group_key="all"):
    s = MagicMock()
    s.group_type = group_type
    s.group_key = group_key
    s.sample_count = 50
    s.avg_return_pct = 1.5
    s.median_return_pct = 1.2
    s.win_rate_pct = 60.0
    s.avg_mae = -3.0
    s.avg_mfe = 5.0
    s.avg_drawdown = -2.0
    s.top_k_hit_rate = 0.7
    s.leader_pool_win_share = 0.7
    s.excess_return_pct = 1.0
    s.ranking_consistency = 0.8
    s.p25_return_pct = -0.5
    s.p75_return_pct = 3.0
    s.extreme_sample_ratio = 0.05
    s.time_bucket_stability = 0.1
    s.to_dict = MagicMock(return_value={
        "group_type": group_type,
        "group_key": group_key,
        "sample_count": 50,
        "avg_return_pct": 1.5,
        "median_return_pct": 1.2,
        "win_rate_pct": 60.0,
        "avg_mae": -3.0,
        "avg_mfe": 5.0,
        "avg_drawdown": -2.0,
        "top_k_hit_rate": 0.7,
        "leader_pool_win_share": 0.7,
        "excess_return_pct": 1.0,
        "ranking_consistency": 0.8,
        "p25_return_pct": -0.5,
        "p75_return_pct": 3.0,
        "extreme_sample_ratio": 0.05,
        "time_bucket_stability": 0.1,
        "profit_factor": 1.8,
        "avg_holding_days": 4.5,
        "max_consecutive_losses": 3,
        "plan_execution_rate": 0.6,
        "stage_accuracy_rate": 0.7,
        "system_grade": "A",
        "metrics_json": json.dumps({
            "sample_baseline": {
                "raw_sample_count": 50,
                "aggregatable_sample_count": 30,
                "entry_sample_count": 30,
                "observation_sample_count": 20,
                "suppressed_sample_count": 20,
                "suppressed_reasons": {
                    "missing_forward_return_5d": 12,
                    "missing_risk_avoided_pct": 8,
                },
            },
            "threshold_check": {
                "can_display": True,
                "can_suggest": True,
                "can_action": False,
                "reason": "sample_count=30 < ACTIONABLE_MIN=50: hypothesis max",
            },
            "family_breakdown": {
                "entry": {"sample_count": 30, "avg_return_pct": 2.4},
                "observation": {"sample_count": 20, "avg_return_pct": 1.1},
            },
            "strategy_cohort_context": {
                "primary_strategy": "ma100_low123_combined",
                "sample_bucket": "core",
                "snapshot_market_regime": "balanced",
                "snapshot_candidate_pool_level": "leader_pool",
                "snapshot_entry_maturity": "high",
            },
        }),
    })
    return s


def _mock_ranking_report():
    return RankingEffectivenessReport(
        comparisons=[
            RankingComparisonResult(
                dimension="entry_maturity",
                tier_high="HIGH",
                tier_low="LOW",
                high_avg_return=3.2,
                low_avg_return=1.1,
                excess_return_pct=2.1,
                high_win_rate=66.0,
                low_win_rate=45.0,
                high_sample_count=12,
                low_sample_count=10,
                is_effective=True,
            ),
        ],
        overall_effectiveness_ratio=0.75,
        leader_pool_win_share=0.6,
        excess_return_pct=1.2,
        ranking_consistency=0.8,
        top_k_hit_rate=0.6,
    )


def _mock_recommendation():
    r = MagicMock()
    r.recommendation_type = "weight_increase"
    r.target_scope = "setup_type"
    r.target_key = "trend_breakout"
    r.recommendation_level = "hypothesis"
    r.suggested_change = "increase weight by 10%"
    r.sample_count = 30
    r.confidence = 0.75
    r.validation_status = "pending"
    r.to_dict = MagicMock(return_value={
        "recommendation_type": "weight_increase",
        "target_scope": "setup_type",
        "target_key": "trend_breakout",
        "recommendation_level": "hypothesis",
        "suggested_change": "increase weight by 10%",
        "sample_count": 30,
        "confidence": 0.75,
        "validation_status": "pending",
    })
    return r


def _mock_calibration():
    c = MagicMock()
    c.to_dict = MagicMock(return_value={
        "calibration_name": "test_cal",
        "decision": "accept",
        "confidence": 0.85,
    })
    return c


# ── API Tests ──────────────────────────────────────────────────────────────

class TestGetRunDetail:
    @patch("api.v1.endpoints.five_layer_backtest.FiveLayerBacktestService")
    def test_get_run_ok(self, MockService, client):
        svc = MockService.return_value
        svc.get_run.return_value = _mock_run()

        resp = client.get("/five-layer-backtest/runs/flbt-test123456")
        assert resp.status_code == 200
        data = resp.json()
        assert data["backtest_run_id"] == "flbt-test123456"
        assert data["status"] == "completed"
        assert data["sample_baseline"]["raw_sample_count"] == 50
        assert data["sample_baseline"]["aggregatable_sample_count"] == 30

    @patch("api.v1.endpoints.five_layer_backtest.FiveLayerBacktestService")
    def test_get_run_not_found(self, MockService, client):
        svc = MockService.return_value
        svc.get_run.return_value = None

        resp = client.get("/five-layer-backtest/runs/flbt-nonexist")
        assert resp.status_code == 404


class TestGetEvaluations:
    @patch("api.v1.endpoints.five_layer_backtest.FiveLayerBacktestService")
    def test_get_evaluations_ok(self, MockService, client):
        svc = MockService.return_value
        svc.get_run.return_value = _mock_run()
        svc.eval_repo.get_by_run.return_value = [_mock_evaluation()]

        resp = client.get("/five-layer-backtest/runs/flbt-test123456/evaluations")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["code"] == "600519"

    @patch("api.v1.endpoints.five_layer_backtest.FiveLayerBacktestService")
    def test_get_evaluations_with_filter(self, MockService, client):
        svc = MockService.return_value
        svc.get_run.return_value = _mock_run()
        svc.eval_repo.get_by_run.return_value = [_mock_evaluation()]

        resp = client.get("/five-layer-backtest/runs/flbt-test123456/evaluations?signal_family=entry")
        assert resp.status_code == 200
        svc.eval_repo.get_by_run.assert_called_once_with("flbt-test123456", signal_family="entry")

    @patch("api.v1.endpoints.five_layer_backtest.FiveLayerBacktestService")
    def test_get_evaluations_exposes_evidence_json(self, MockService, client):
        svc = MockService.return_value
        svc.get_run.return_value = _mock_run()
        svc.eval_repo.get_by_run.return_value = [_mock_evaluation()]

        resp = client.get("/five-layer-backtest/runs/flbt-test123456/evaluations")
        assert resp.status_code == 200
        data = resp.json()
        assert "evidence_json" in data["items"][0]

    @patch("api.v1.endpoints.five_layer_backtest.FiveLayerBacktestService")
    def test_get_evaluations_exposes_structured_attribution_fields(self, MockService, client):
        svc = MockService.return_value
        svc.get_run.return_value = _mock_run()
        svc.eval_repo.get_by_run.return_value = [_mock_evaluation()]

        resp = client.get("/five-layer-backtest/runs/flbt-test123456/evaluations")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["primary_strategy"] == "trend_breakout"
        assert item["contributing_strategies"] == ["volume_breakout"]
        assert item["sample_bucket"] == "core"
        assert item["entry_timing_label"] == "on_time"
        assert item["ma100_low123_validation_status"] == "confirmed_missing_breakout_bar_index"
        assert item["ma100_low123_data_complete"] is False

    @patch("api.v1.endpoints.five_layer_backtest.FiveLayerBacktestService")
    def test_get_evaluations_normalizes_invalid_strategy_lists(self, MockService, client):
        svc = MockService.return_value
        svc.get_run.return_value = _mock_run()
        svc.eval_repo.get_by_run.return_value = [_mock_evaluation_with_invalid_strategy_lists()]

        resp = client.get("/five-layer-backtest/runs/flbt-test123456/evaluations")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["matched_strategies"] == []
        assert item["contributing_strategies"] == []


class TestGetSummaries:
    @patch("api.v1.endpoints.five_layer_backtest.FiveLayerBacktestService")
    def test_get_summaries_ok(self, MockService, client):
        svc = MockService.return_value
        svc.get_run.return_value = _mock_run()
        svc.summary_repo.get_by_run.return_value = [_mock_summary()]

        resp = client.get("/five-layer-backtest/runs/flbt-test123456/summaries")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["group_type"] == "overall"
        assert data["items"][0]["top_k_hit_rate"] == 0.7
        # D2: canonical name is now exposed alongside the legacy alias.
        assert data["items"][0]["leader_pool_win_share"] == 0.7
        assert data["items"][0]["profit_factor"] == 1.8

    @patch("api.v1.endpoints.five_layer_backtest.FiveLayerBacktestService")
    def test_get_summaries_exposes_structured_metrics_fields(self, MockService, client):
        svc = MockService.return_value
        svc.get_run.return_value = _mock_run()
        svc.summary_repo.get_by_run.return_value = [_mock_summary("strategy_cohort", "ps=ma100_low123_combined")]

        resp = client.get("/five-layer-backtest/runs/flbt-test123456/summaries")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["family_breakdown"]["entry"]["sample_count"] == 30
        assert item["strategy_cohort_context"]["primary_strategy"] == "ma100_low123_combined"
        assert item["sample_baseline"]["raw_sample_count"] == 50
        assert item["sample_baseline"]["suppressed_sample_count"] == 20
        assert item["threshold_check"]["can_action"] is False


class TestGetRankingEffectiveness:
    @patch("api.v1.endpoints.five_layer_backtest.FiveLayerBacktestService")
    def test_get_ranking_effectiveness_ok(self, MockService, client):
        svc = MockService.return_value
        svc.get_run.return_value = _mock_run()
        svc.get_ranking_effectiveness.return_value = _mock_ranking_report()

        resp = client.get("/five-layer-backtest/runs/flbt-test123456/ranking-effectiveness")
        assert resp.status_code == 200
        data = resp.json()
        assert data["overall_effectiveness_ratio"] == 0.75
        assert data["comparisons"][0]["tier_high"] == "HIGH"
        # D2: both leader_pool_win_share (canonical) and top_k_hit_rate
        # (deprecated alias) must be exposed and carry the same value so
        # legacy and new clients both keep working through the deprecation
        # window.
        assert data["leader_pool_win_share"] == 0.6
        assert data["top_k_hit_rate"] == 0.6
        assert data["leader_pool_win_share"] == data["top_k_hit_rate"]


class TestGetCalibration:
    @patch("api.v1.endpoints.five_layer_backtest.FiveLayerBacktestService")
    def test_get_calibration_ok(self, MockService, client):
        svc = MockService.return_value
        svc.get_run.return_value = _mock_run()
        svc.calibration_repo.get_by_run.return_value = [_mock_calibration()]

        resp = client.get("/five-layer-backtest/runs/flbt-test123456/calibration")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["decision"] == "accept"


class TestGetRecommendations:
    @patch("api.v1.endpoints.five_layer_backtest.FiveLayerBacktestService")
    def test_get_recommendations_ok(self, MockService, client):
        svc = MockService.return_value
        svc.get_run.return_value = _mock_run()
        svc.recommendation_repo.get_by_run.return_value = [_mock_recommendation()]

        resp = client.get("/five-layer-backtest/runs/flbt-test123456/recommendations")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["recommendation_level"] == "hypothesis"

    @patch("api.v1.endpoints.five_layer_backtest.FiveLayerBacktestService")
    def test_get_recommendations_with_level_filter(self, MockService, client):
        svc = MockService.return_value
        svc.get_run.return_value = _mock_run()
        svc.recommendation_repo.get_by_run.return_value = []

        resp = client.get("/five-layer-backtest/runs/flbt-test123456/recommendations?recommendation_level=actionable")
        assert resp.status_code == 200
        svc.recommendation_repo.get_by_run.assert_called_once_with("flbt-test123456", recommendation_level="actionable")


class TestRunBacktest:
    @patch("api.v1.endpoints.five_layer_backtest.FiveLayerBacktestService")
    def test_run_ok(self, MockService, client):
        svc = MockService.return_value
        svc.run_full_pipeline.return_value = {
            "run": _mock_run(),
            "summaries": [_mock_summary()],
            "recommendations": [_mock_recommendation()],
        }

        resp = client.post("/five-layer-backtest/run", json={
            "trade_date_from": "2026-03-01",
            "trade_date_to": "2026-03-31",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["run"]["backtest_run_id"] == "flbt-test123456"
        assert len(data["summaries"]) == 1
        assert data["summaries"][0]["ranking_consistency"] == 0.8
        assert len(data["recommendations"]) == 1

    @patch("api.v1.endpoints.five_layer_backtest.FiveLayerBacktestService")
    def test_run_bad_date(self, MockService, client):
        resp = client.post("/five-layer-backtest/run", json={
            "trade_date_from": "2026-03-31",
            "trade_date_to": "2026-03-01",
        })
        assert resp.status_code == 400

    @patch("api.v1.endpoints.five_layer_backtest.FiveLayerBacktestService")
    def test_run_invalid_date_format(self, MockService, client):
        resp = client.post("/five-layer-backtest/run", json={
            "trade_date_from": "not-a-date",
            "trade_date_to": "2026-03-31",
        })
        assert resp.status_code == 400

    @patch("api.v1.endpoints.five_layer_backtest.FiveLayerBacktestService")
    def test_run_internal_error_hides_exception_details(self, MockService, client):
        svc = MockService.return_value
        svc.run_full_pipeline.side_effect = RuntimeError("sqlalchemy failed: secret details")

        resp = client.post("/five-layer-backtest/run", json={
            "trade_date_from": "2026-03-01",
            "trade_date_to": "2026-03-31",
        })

        assert resp.status_code == 500
        data = resp.json()["detail"]
        assert data["error"] == "internal_error"
        assert data["message"] == "五层回测执行失败"


class TestRunByScreeningRun:
    @patch("api.v1.endpoints.five_layer_backtest.FiveLayerBacktestService")
    def test_run_by_screening_run_ok(self, MockService, client):
        svc = MockService.return_value
        svc.run_backtest_pipeline.return_value = _mock_run("flbt-screening123")
        svc.compute_summaries.return_value = [_mock_summary()]
        svc.generate_recommendations.return_value = [_mock_recommendation()]

        resp = client.post("/five-layer-backtest/run/by-screening-run", json={
            "screening_run_id": "sr-20260415-001",
            "evaluation_mode": "historical_snapshot",
            "execution_model": "conservative",
            "market": "cn",
            "eval_window_days": 10,
            "generate_recommendations": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["run"]["backtest_run_id"] == "flbt-screening123"
        svc.run_backtest_pipeline.assert_called_once_with(
            screening_run_id="sr-20260415-001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
            market="cn",
            eval_window_days=10,
        )

    def test_run_by_screening_run_validation_error(self, client):
        resp = client.post("/five-layer-backtest/run/by-screening-run", json={
            "screening_run_id": "",
        })
        assert resp.status_code == 422


class TestRunCalibration:
    @patch("api.v1.endpoints.five_layer_backtest.FiveLayerBacktestService")
    def test_calibration_ok(self, MockService, client):
        svc = MockService.return_value
        svc.get_run.return_value = _mock_run()
        svc.run_calibration_comparison.return_value = _mock_calibration()

        resp = client.post("/five-layer-backtest/calibration", json={
            "baseline_run_id": "flbt-base1",
            "candidate_run_id": "flbt-cand1",
            "calibration_name": "test_cal",
        })
        assert resp.status_code == 200
        assert resp.json()["decision"] == "accept"

    @patch("api.v1.endpoints.five_layer_backtest.FiveLayerBacktestService")
    def test_calibration_run_not_found(self, MockService, client):
        svc = MockService.return_value
        svc.get_run.return_value = None

        resp = client.post("/five-layer-backtest/calibration", json={
            "baseline_run_id": "flbt-missing",
            "candidate_run_id": "flbt-cand1",
            "calibration_name": "test_cal",
        })
        assert resp.status_code == 404


# ── Agent Tool Tests ───────────────────────────────────────────────────────

class TestAgentTools:
    @patch("src.agent.tools.five_layer_backtest_tools._get_service")
    def test_get_run_summary_ok(self, mock_get_svc):
        from src.agent.tools.five_layer_backtest_tools import _handle_get_run_summary

        svc = MagicMock()
        mock_get_svc.return_value = svc
        svc.get_run.return_value = _mock_run()
        svc.summary_repo.get_by_run.return_value = [_mock_summary()]

        result = _handle_get_run_summary("flbt-test123456")
        assert result["backtest_run_id"] == "flbt-test123456"
        assert "overall_summary" in result

    @patch("src.agent.tools.five_layer_backtest_tools._get_service")
    def test_get_run_summary_not_found(self, mock_get_svc):
        from src.agent.tools.five_layer_backtest_tools import _handle_get_run_summary

        svc = MagicMock()
        mock_get_svc.return_value = svc
        svc.get_run.return_value = None

        result = _handle_get_run_summary("flbt-missing")
        assert "info" in result

    @patch("src.agent.tools.five_layer_backtest_tools._get_service")
    def test_get_group_summary_ok(self, mock_get_svc):
        from src.agent.tools.five_layer_backtest_tools import _handle_get_group_summary

        svc = MagicMock()
        mock_get_svc.return_value = svc
        svc.get_run.return_value = _mock_run()
        svc.summary_repo.get_by_run.return_value = [_mock_summary("signal_family", "entry")]

        result = _handle_get_group_summary("flbt-test123456", "signal_family")
        assert "summaries" in result
        assert len(result["summaries"]) == 1

    @patch("src.agent.tools.five_layer_backtest_tools._get_service")
    def test_get_recommendations_ok(self, mock_get_svc):
        from src.agent.tools.five_layer_backtest_tools import _handle_get_recommendations

        svc = MagicMock()
        mock_get_svc.return_value = svc
        svc.get_run.return_value = _mock_run()
        svc.recommendation_repo.get_by_run.return_value = [_mock_recommendation()]

        result = _handle_get_recommendations("flbt-test123456")
        assert len(result["recommendations"]) == 1
        assert result["recommendations"][0]["recommendation_level"] == "hypothesis"

    @patch("src.agent.tools.five_layer_backtest_tools._get_service")
    def test_get_candidate_detail_ok(self, mock_get_svc):
        from src.agent.tools.five_layer_backtest_tools import _handle_get_candidate_detail

        svc = MagicMock()
        mock_get_svc.return_value = svc
        svc.get_run.return_value = _mock_run()
        svc.eval_repo.get_by_run.return_value = [_mock_evaluation("600519")]

        result = _handle_get_candidate_detail("flbt-test123456", "600519")
        assert len(result["evaluations"]) == 1
        assert result["code"] == "600519"

    @patch("src.agent.tools.five_layer_backtest_tools._get_service")
    def test_get_candidate_detail_not_found(self, mock_get_svc):
        from src.agent.tools.five_layer_backtest_tools import _handle_get_candidate_detail

        svc = MagicMock()
        mock_get_svc.return_value = svc
        svc.get_run.return_value = _mock_run()
        svc.eval_repo.get_by_run.return_value = [_mock_evaluation("600519")]

        result = _handle_get_candidate_detail("flbt-test123456", "000001")
        assert "info" in result


# ── Schema Tests ───────────────────────────────────────────────────────────

class TestSchemas:
    def test_run_request_defaults(self):
        req = FiveLayerBacktestRunRequest(
            trade_date_from="2026-03-01",
            trade_date_to="2026-03-31",
        )
        assert req.evaluation_mode == "historical_snapshot"
        assert req.execution_model == "conservative"
        assert req.market == "cn"
        assert req.eval_window_days == 10
        assert req.generate_recommendations is True

    def test_calibration_request(self):
        req = FiveLayerCalibrationRequest(
            baseline_run_id="flbt-base1",
            candidate_run_id="flbt-cand1",
            calibration_name="test",
        )
        assert req.baseline_config is None

    def test_tool_definitions(self):
        from src.agent.tools.five_layer_backtest_tools import ALL_FIVE_LAYER_BACKTEST_TOOLS
        assert len(ALL_FIVE_LAYER_BACKTEST_TOOLS) == 4
        names = {t.name for t in ALL_FIVE_LAYER_BACKTEST_TOOLS}
        assert "get_five_layer_backtest_run_summary" in names
        assert "get_five_layer_group_summary" in names
        assert "get_five_layer_recommendations" in names
        assert "get_five_layer_candidate_detail" in names

    def test_tool_to_openai(self):
        from src.agent.tools.five_layer_backtest_tools import ALL_FIVE_LAYER_BACKTEST_TOOLS
        for t in ALL_FIVE_LAYER_BACKTEST_TOOLS:
            schema = t.to_openai_tool()
            assert schema["type"] == "function"
            assert "parameters" in schema["function"]

    def test_group_summary_schema_accepts_new_fields(self):
        item = FiveLayerGroupSummaryItem(
            group_type="overall",
            group_key="all",
            sample_count=20,
            profit_factor=1.8,
            avg_holding_days=4.5,
            max_consecutive_losses=2,
            plan_execution_rate=0.55,
            stage_accuracy_rate=0.65,
            system_grade="A",
        )

        assert item.profit_factor == 1.8
        assert item.system_grade == "A"

    def test_evaluation_schema_accepts_detail_fields(self):
        item = FiveLayerEvaluationItem(
            backtest_run_id="flbt-test123456",
            code="600519",
            signal_family="entry",
            evaluator_type="entry",
            signal_type="buy",
            factor_snapshot_json='{"ma100_breakout_days": 3}',
            trade_plan_json='{"take_profit": 5}',
        )

        assert item.signal_type == "buy"
        assert "ma100_breakout_days" in item.factor_snapshot_json
