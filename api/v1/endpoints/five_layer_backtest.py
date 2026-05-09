# -*- coding: utf-8 -*-
"""Five-layer backtest API endpoints."""
from __future__ import annotations

import logging
import json
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import get_database_manager
from api.v1.schemas.common import ErrorResponse
from api.v1.schemas.five_layer_backtest import (
    FiveLayerBacktestRunRequest,
    FiveLayerBacktestScreeningRunRequest,
    FiveLayerCalibrationItem,
    FiveLayerCalibrationRequest,
    FiveLayerCalibrationResponse,
    FiveLayerEvaluationItem,
    FiveLayerEvaluationsResponse,
    FiveLayerFullPipelineResponse,
    FiveLayerGroupSummaryItem,
    FiveLayerRecommendationItem,
    FiveLayerRecommendationsResponse,
    FiveLayerRunResponse,
    FiveLayerSummariesResponse,
    RankingComparisonItem,
    RankingEffectivenessResponse,
)
from src.backtest.services.backtest_service import FiveLayerBacktestService
from src.storage import DatabaseManager

logger = logging.getLogger(__name__)

router = APIRouter()


def _internal_error(message: str) -> HTTPException:
    return HTTPException(
        status_code=500,
        detail={"error": "internal_error", "message": message},
    )


def _parse_date(value: str, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation_error",
                "message": f"Invalid date format for {field_name}: {value}. Expected YYYY-MM-DD.",
            },
        ) from exc


def _run_to_response(run) -> FiveLayerRunResponse:
    payload = run.to_dict()
    config = _parse_json_dict(payload.get("config_json"), field_name="config_json")
    sample_baseline = config.get("sample_baseline")
    candidate_filter = _parse_json_dict(
        payload.get("candidate_filter_json"),
        field_name="candidate_filter_json",
    ) or None
    payload.update(
        {
            "sample_baseline": sample_baseline if isinstance(sample_baseline, dict) else None,
            "candidate_filter": candidate_filter if isinstance(candidate_filter, dict) else None,
        }
    )
    return FiveLayerRunResponse(**payload)


def _parse_json_dict(payload: str | None, field_name: str = "json_field") -> dict:
    if not payload:
        return {}
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.warning("Failed to parse %s for five-layer backtest response", field_name)
        return {}
    if not isinstance(parsed, dict):
        logger.warning("Unexpected non-dict payload for %s in five-layer backtest response", field_name)
        return {}
    return parsed


def _parse_json_list(payload) -> list:
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, str)]


def _evaluation_to_response(evaluation) -> FiveLayerEvaluationItem:
    payload = evaluation.to_dict()
    evidence = _parse_json_dict(payload.get("evidence_json"), field_name="evidence_json")
    metrics = _parse_json_dict(payload.get("metrics_json"), field_name="metrics_json")
    factor_snapshot = _parse_json_dict(
        payload.get("factor_snapshot_json"),
        field_name="factor_snapshot_json",
    )
    payload.update(
        {
            "primary_strategy": evidence.get("primary_strategy"),
            "contributing_strategies": _parse_json_list(evidence.get("contributing_strategies")),
            "matched_strategies": _parse_json_list(evidence.get("matched_strategies")),
            "rule_hits": _parse_json_list(evidence.get("rule_hits")),
            "sample_bucket": metrics.get("sample_bucket"),
            "entry_timing_label": metrics.get("entry_timing_label"),
            "ma100_low123_validation_status": factor_snapshot.get("ma100_low123_validation_status"),
            "ma100_low123_data_complete": factor_snapshot.get("ma100_low123_data_complete"),
        }
    )
    return FiveLayerEvaluationItem(**payload)


def _summary_to_response(summary) -> FiveLayerGroupSummaryItem:
    payload = summary.to_dict()
    metrics = _parse_json_dict(payload.get("metrics_json"), field_name="metrics_json")
    sample_baseline = metrics.get("sample_baseline")
    threshold_check = metrics.get("threshold_check")
    family_breakdown = metrics.get("family_breakdown")
    strategy_cohort_context = metrics.get("strategy_cohort_context")
    payload.update(
        {
            "sample_baseline": sample_baseline if isinstance(sample_baseline, dict) else None,
            "threshold_check": threshold_check if isinstance(threshold_check, dict) else None,
            "family_breakdown": family_breakdown if isinstance(family_breakdown, dict) else None,
            "strategy_cohort_context": (
                strategy_cohort_context if isinstance(strategy_cohort_context, dict) else None
            ),
        }
    )
    return FiveLayerGroupSummaryItem(**payload)


def _ranking_to_response(report) -> RankingEffectivenessResponse:
    # D2: keep both ``top_k_hit_rate`` (legacy alias) and
    #     ``leader_pool_win_share`` (canonical) in the payload.
    # D1: also surface family_scope plus the mixed-legacy parallel values so
    #     analysts can audit how much the family-mix bias was inflating the
    #     pre-D1 numbers.
    win_share = getattr(report, "leader_pool_win_share", None)
    if win_share is None:
        win_share = getattr(report, "top_k_hit_rate", None)
    return RankingEffectivenessResponse(
        comparisons=[
            RankingComparisonItem(**comparison.__dict__)
            for comparison in (report.comparisons or [])
        ],
        overall_effectiveness_ratio=report.overall_effectiveness_ratio,
        top_k_hit_rate=win_share,
        leader_pool_win_share=win_share,
        excess_return_pct=report.excess_return_pct,
        ranking_consistency=report.ranking_consistency,
        family_scope=getattr(report, "family_scope", None),
        leader_pool_win_share_mixed=getattr(report, "leader_pool_win_share_mixed", None),
        excess_return_pct_mixed=getattr(report, "excess_return_pct_mixed", None),
    )


@router.post(
    "/run",
    response_model=FiveLayerFullPipelineResponse,
    responses={
        400: {"description": "Invalid request", "model": ErrorResponse},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Run five-layer backtest pipeline",
)
def run_five_layer_backtest(
    request: FiveLayerBacktestRunRequest,
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> FiveLayerFullPipelineResponse:
    date_from = _parse_date(request.trade_date_from, "trade_date_from")
    date_to = _parse_date(request.trade_date_to, "trade_date_to")
    if date_from > date_to:
        raise HTTPException(
            status_code=400,
            detail={"error": "validation_error", "message": "trade_date_from cannot be later than trade_date_to"},
        )

    try:
        service = FiveLayerBacktestService(db_manager)
        result = service.run_full_pipeline(
            evaluation_mode=request.evaluation_mode,
            execution_model=request.execution_model,
            trade_date_from=date_from,
            trade_date_to=date_to,
            market=request.market,
            eval_window_days=request.eval_window_days,
            generate_recommendations=request.generate_recommendations,
        )
        return FiveLayerFullPipelineResponse(
            run=_run_to_response(result["run"]),
            summaries=[
                _summary_to_response(summary)
                for summary in (result.get("summaries") or [])
            ],
            recommendations=[
                FiveLayerRecommendationItem(**recommendation.to_dict())
                for recommendation in (result.get("recommendations") or [])
            ],
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to run five-layer backtest: %s", exc, exc_info=True)
        raise _internal_error("五层回测执行失败")


@router.post(
    "/run/by-screening-run",
    response_model=FiveLayerFullPipelineResponse,
    responses={
        400: {"description": "Invalid request", "model": ErrorResponse},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Run five-layer backtest for one screening run",
)
def run_five_layer_backtest_by_screening_run(
    request: FiveLayerBacktestScreeningRunRequest,
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> FiveLayerFullPipelineResponse:
    try:
        service = FiveLayerBacktestService(db_manager)
        run = service.run_backtest_pipeline(
            screening_run_id=request.screening_run_id,
            evaluation_mode=request.evaluation_mode,
            execution_model=request.execution_model,
            market=request.market,
            eval_window_days=request.eval_window_days,
        )
        summaries = service.compute_summaries(run.backtest_run_id)
        recommendations = []
        if request.generate_recommendations and summaries:
            recommendations = service.generate_recommendations(run.backtest_run_id)
        return FiveLayerFullPipelineResponse(
            run=_run_to_response(run),
            summaries=[
                _summary_to_response(summary)
                for summary in summaries
            ],
            recommendations=[
                FiveLayerRecommendationItem(**recommendation.to_dict())
                for recommendation in recommendations
            ],
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Failed to run five-layer backtest by screening run: %s",
            exc,
            exc_info=True,
        )
        raise _internal_error("五层回测执行失败")


@router.get(
    "/runs/{backtest_run_id}",
    response_model=FiveLayerRunResponse,
    responses={
        404: {"description": "Run not found", "model": ErrorResponse},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Get run detail",
)
def get_run_detail(
    backtest_run_id: str,
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> FiveLayerRunResponse:
    try:
        service = FiveLayerBacktestService(db_manager)
        run = service.get_run(backtest_run_id)
        if run is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": f"Run not found: {backtest_run_id}"},
            )
        return _run_to_response(run)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to fetch run detail: %s", exc, exc_info=True)
        raise _internal_error("Failed to fetch run detail")


@router.get(
    "/runs/{backtest_run_id}/evaluations",
    response_model=FiveLayerEvaluationsResponse,
    responses={
        404: {"description": "Run not found", "model": ErrorResponse},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Get candidate evaluations",
)
def get_evaluations(
    backtest_run_id: str,
    signal_family: str | None = Query(None, description="entry / exit / observation"),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> FiveLayerEvaluationsResponse:
    try:
        service = FiveLayerBacktestService(db_manager)
        if service.get_run(backtest_run_id) is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": f"Run not found: {backtest_run_id}"},
            )

        evaluations = service.eval_repo.get_by_run(backtest_run_id, signal_family=signal_family)
        total = len(evaluations)
        start = (page - 1) * limit
        items = [
            _evaluation_to_response(evaluation)
            for evaluation in evaluations[start : start + limit]
        ]
        return FiveLayerEvaluationsResponse(
            backtest_run_id=backtest_run_id,
            total=total,
            page=page,
            limit=limit,
            items=items,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to fetch evaluations: %s", exc, exc_info=True)
        raise _internal_error("Failed to fetch evaluations")


@router.get(
    "/runs/{backtest_run_id}/summaries",
    response_model=FiveLayerSummariesResponse,
    responses={
        404: {"description": "Run not found", "model": ErrorResponse},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Get group summaries",
)
def get_summaries(
    backtest_run_id: str,
    group_type: str | None = Query(None, description="overall / signal_family / setup_type / ..."),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> FiveLayerSummariesResponse:
    try:
        service = FiveLayerBacktestService(db_manager)
        if service.get_run(backtest_run_id) is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": f"Run not found: {backtest_run_id}"},
            )

        items = [
            _summary_to_response(summary)
            for summary in service.summary_repo.get_by_run(backtest_run_id, group_type=group_type)
        ]
        return FiveLayerSummariesResponse(backtest_run_id=backtest_run_id, items=items)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to fetch summaries: %s", exc, exc_info=True)
        raise _internal_error("Failed to fetch summaries")


@router.get(
    "/runs/{backtest_run_id}/ranking-effectiveness",
    response_model=RankingEffectivenessResponse,
    responses={
        404: {"description": "Run or ranking report not found", "model": ErrorResponse},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Get ranking effectiveness",
)
def get_ranking_effectiveness(
    backtest_run_id: str,
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> RankingEffectivenessResponse:
    try:
        service = FiveLayerBacktestService(db_manager)
        if service.get_run(backtest_run_id) is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": f"Run not found: {backtest_run_id}"},
            )

        report = service.get_ranking_effectiveness(backtest_run_id)
        if report is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "not_found",
                    "message": f"Ranking effectiveness not found for run: {backtest_run_id}",
                },
            )
        return _ranking_to_response(report)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to fetch ranking effectiveness: %s", exc, exc_info=True)
        raise _internal_error("Failed to fetch ranking effectiveness")


@router.get(
    "/runs/{backtest_run_id}/calibration",
    response_model=FiveLayerCalibrationResponse,
    responses={
        404: {"description": "Run not found", "model": ErrorResponse},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Get calibration outputs",
)
def get_calibration(
    backtest_run_id: str,
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> FiveLayerCalibrationResponse:
    try:
        service = FiveLayerBacktestService(db_manager)
        if service.get_run(backtest_run_id) is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": f"Run not found: {backtest_run_id}"},
            )

        items = [
            FiveLayerCalibrationItem(**output.to_dict())
            for output in service.calibration_repo.get_by_run(backtest_run_id)
        ]
        return FiveLayerCalibrationResponse(backtest_run_id=backtest_run_id, items=items)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to fetch calibration outputs: %s", exc, exc_info=True)
        raise _internal_error("Failed to fetch calibration outputs")


@router.get(
    "/runs/{backtest_run_id}/recommendations",
    response_model=FiveLayerRecommendationsResponse,
    responses={
        404: {"description": "Run not found", "model": ErrorResponse},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Get recommendations",
)
def get_recommendations(
    backtest_run_id: str,
    recommendation_level: str | None = Query(None, description="observation / hypothesis / actionable"),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> FiveLayerRecommendationsResponse:
    try:
        service = FiveLayerBacktestService(db_manager)
        if service.get_run(backtest_run_id) is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": f"Run not found: {backtest_run_id}"},
            )

        items = [
            FiveLayerRecommendationItem(**recommendation.to_dict())
            for recommendation in service.recommendation_repo.get_by_run(
                backtest_run_id,
                recommendation_level=recommendation_level,
            )
        ]
        return FiveLayerRecommendationsResponse(backtest_run_id=backtest_run_id, items=items)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to fetch recommendations: %s", exc, exc_info=True)
        raise _internal_error("Failed to fetch recommendations")


@router.post(
    "/calibration",
    response_model=FiveLayerCalibrationItem,
    responses={
        400: {"description": "Invalid request", "model": ErrorResponse},
        404: {"description": "Run not found", "model": ErrorResponse},
        500: {"description": "Internal server error", "model": ErrorResponse},
    },
    summary="Run calibration comparison",
)
def run_calibration(
    request: FiveLayerCalibrationRequest,
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> FiveLayerCalibrationItem:
    try:
        service = FiveLayerBacktestService(db_manager)
        for run_id in (request.baseline_run_id, request.candidate_run_id):
            if service.get_run(run_id) is None:
                raise HTTPException(
                    status_code=404,
                    detail={"error": "not_found", "message": f"Run not found: {run_id}"},
                )

        output = service.run_calibration_comparison(
            baseline_run_id=request.baseline_run_id,
            candidate_run_id=request.candidate_run_id,
            calibration_name=request.calibration_name,
            baseline_config=request.baseline_config,
            candidate_config=request.candidate_config,
        )
        if output is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "calibration_failed",
                    "message": "Calibration comparison failed. Ensure both runs have summaries.",
                },
            )
        return FiveLayerCalibrationItem(**output.to_dict())
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to run calibration comparison: %s", exc, exc_info=True)
        raise _internal_error("Calibration comparison failed")
