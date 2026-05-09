# -*- coding: utf-8 -*-
"""Five-layer backtest API schemas.

Request and response models for the run-based five-layer backtest system.
All endpoints use ``backtest_run_id`` as the primary index.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Request models ─────────────────────────────────────────────────────────

class FiveLayerBacktestRunRequest(BaseModel):
    evaluation_mode: str = Field(
        "historical_snapshot",
        description="评估模式: historical_snapshot / rule_replay / parameter_calibration",
    )
    execution_model: str = Field(
        "conservative",
        description="执行模型: conservative / baseline / optimistic",
    )
    trade_date_from: str = Field(..., description="回测起始日期 (YYYY-MM-DD)")
    trade_date_to: str = Field(..., description="回测结束日期 (YYYY-MM-DD)")
    market: str = Field("cn", description="市场: cn / us / hk")
    eval_window_days: int = Field(10, ge=1, le=120, description="评估窗口（交易日数）")
    generate_recommendations: bool = Field(True, description="是否生成建议")


class FiveLayerBacktestScreeningRunRequest(BaseModel):
    screening_run_id: str = Field(..., min_length=1, description="选股运行ID")
    evaluation_mode: str = Field(
        "historical_snapshot",
        description="评估模式: historical_snapshot / rule_replay / parameter_calibration",
    )
    execution_model: str = Field(
        "conservative",
        description="执行模型: conservative / baseline / optimistic",
    )
    market: str = Field("cn", description="市场: cn / us / hk")
    eval_window_days: int = Field(10, ge=1, le=120, description="评估窗口（交易日数）")
    generate_recommendations: bool = Field(True, description="是否生成建议")


class FiveLayerCalibrationRequest(BaseModel):
    baseline_run_id: str = Field(..., description="基准运行ID")
    candidate_run_id: str = Field(..., description="候选运行ID")
    calibration_name: str = Field(..., description="校准名称")
    baseline_config: Optional[Dict[str, Any]] = Field(None, description="基准配置")
    candidate_config: Optional[Dict[str, Any]] = Field(None, description="候选配置")


# ── Response models ────────────────────────────────────────────────────────

class SampleBaselineResponse(BaseModel):
    raw_sample_count: int = 0
    evaluated_sample_count: Optional[int] = None
    aggregatable_sample_count: int = 0
    entry_sample_count: int = 0
    observation_sample_count: int = 0
    suppressed_sample_count: int = 0
    suppressed_reasons: Dict[str, int] = Field(default_factory=dict)


class ThresholdCheckResponse(BaseModel):
    can_display: bool
    can_suggest: bool
    can_action: bool
    reason: Optional[str] = None

class FiveLayerRunResponse(BaseModel):
    backtest_run_id: str
    evaluation_mode: str
    execution_model: str
    trade_date_from: Optional[str] = None
    trade_date_to: Optional[str] = None
    market: str = "cn"
    status: str = "pending"
    sample_count: int = 0
    completed_count: int = 0
    error_count: int = 0
    data_version: Optional[str] = None
    market_data_version: Optional[str] = None
    theme_mapping_version: Optional[str] = None
    candidate_snapshot_version: Optional[str] = None
    rules_version: Optional[str] = None
    sample_baseline: Optional[SampleBaselineResponse] = None
    # A1: structured filter snapshot — source / screening_run_ids / market /
    # evaluation_mode / execution_model / eval_window_days. Lets API consumers
    # answer "which screening runs did this backtest cover?" without re-querying
    # screening tables by trade date.
    candidate_filter: Optional[Dict[str, Any]] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


class FiveLayerEvaluationItem(BaseModel):
    id: Optional[int] = None
    backtest_run_id: str
    trade_date: Optional[str] = None
    code: str
    name: Optional[str] = None
    signal_family: str
    signal_type: Optional[str] = None
    evaluator_type: str
    evaluation_mode: Optional[str] = None
    execution_model: Optional[str] = None
    snapshot_source: Optional[str] = None
    replayed: Optional[bool] = None

    # Snapshot fields
    snapshot_trade_stage: Optional[str] = None
    snapshot_setup_type: Optional[str] = None
    snapshot_entry_maturity: Optional[str] = None
    snapshot_market_regime: Optional[str] = None
    snapshot_theme_position: Optional[str] = None
    snapshot_candidate_pool_level: Optional[str] = None
    snapshot_risk_level: Optional[str] = None

    # Execution
    entry_fill_status: Optional[str] = None
    entry_fill_price: Optional[float] = None
    exit_fill_price: Optional[float] = None
    limit_blocked: Optional[bool] = None
    gap_adjusted: Optional[bool] = None

    # Metrics
    forward_return_1d: Optional[float] = None
    forward_return_3d: Optional[float] = None
    forward_return_5d: Optional[float] = None
    forward_return_10d: Optional[float] = None
    mae: Optional[float] = None
    mfe: Optional[float] = None
    max_drawdown_from_peak: Optional[float] = None
    holding_days: Optional[int] = None
    plan_success: Optional[bool] = None
    signal_quality_score: Optional[float] = None
    risk_avoided_pct: Optional[float] = None
    opportunity_cost_pct: Optional[float] = None
    outcome: Optional[str] = None
    stage_success: Optional[bool] = None
    eval_status: Optional[str] = None
    suppression_reason: Optional[str] = None
    metrics_json: Optional[str] = None
    evidence_json: Optional[str] = None
    factor_snapshot_json: Optional[str] = None
    trade_plan_json: Optional[str] = None
    primary_strategy: Optional[str] = None
    contributing_strategies: List[str] = Field(default_factory=list)
    matched_strategies: List[str] = Field(default_factory=list)
    rule_hits: List[str] = Field(default_factory=list)
    sample_bucket: Optional[str] = None
    entry_timing_label: Optional[str] = None
    ma100_low123_validation_status: Optional[str] = None
    ma100_low123_data_complete: Optional[bool] = None


class FiveLayerEvaluationsResponse(BaseModel):
    backtest_run_id: str
    total: int
    page: int
    limit: int
    items: List[FiveLayerEvaluationItem] = Field(default_factory=list)


class FiveLayerGroupSummaryItem(BaseModel):
    group_type: str
    group_key: str
    sample_count: int = 0
    avg_return_pct: Optional[float] = None
    median_return_pct: Optional[float] = None
    win_rate_pct: Optional[float] = None
    avg_mae: Optional[float] = None
    avg_mfe: Optional[float] = None
    avg_drawdown: Optional[float] = None
    # ``top_k_hit_rate`` is a DEPRECATED alias kept for backward
    # compatibility. Prefer ``leader_pool_win_share`` in new clients.
    top_k_hit_rate: Optional[float] = None
    leader_pool_win_share: Optional[float] = None
    excess_return_pct: Optional[float] = None
    ranking_consistency: Optional[float] = None
    p25_return_pct: Optional[float] = None
    p75_return_pct: Optional[float] = None
    extreme_sample_ratio: Optional[float] = None
    time_bucket_stability: Optional[float] = None
    profit_factor: Optional[float] = None
    avg_holding_days: Optional[float] = None
    max_consecutive_losses: Optional[int] = None
    plan_execution_rate: Optional[float] = None
    stage_accuracy_rate: Optional[float] = None
    system_grade: Optional[str] = None
    metrics_json: Optional[str] = None
    sample_baseline: Optional[SampleBaselineResponse] = None
    threshold_check: Optional[ThresholdCheckResponse] = None
    family_breakdown: Optional[Dict[str, Dict[str, Any]]] = None
    strategy_cohort_context: Optional[Dict[str, Any]] = None


class RankingComparisonItem(BaseModel):
    dimension: str
    tier_high: str
    tier_low: str
    high_avg_return: Optional[float] = None
    low_avg_return: Optional[float] = None
    excess_return_pct: Optional[float] = None
    high_win_rate: Optional[float] = None
    low_win_rate: Optional[float] = None
    high_sample_count: int
    low_sample_count: int
    is_effective: bool
    # D1: ``family_entry`` / ``family_observation`` mean the comparison was
    # anchored on the family-correct numbers from family_breakdown;
    # ``mixed_legacy`` means it reproduced the pre-D1 mixed-summary read;
    # ``mixed_fallback`` means at least one tier lacked the requested family
    # and the comparison silently fell back to mixed columns.
    metric_source: Optional[str] = None


class RankingEffectivenessResponse(BaseModel):
    comparisons: List[RankingComparisonItem] = Field(default_factory=list)
    overall_effectiveness_ratio: float
    # ``top_k_hit_rate`` is a DEPRECATED alias of ``leader_pool_win_share``.
    # Both fields hold identical values in current responses.
    top_k_hit_rate: Optional[float] = None
    leader_pool_win_share: Optional[float] = None
    excess_return_pct: Optional[float] = None
    ranking_consistency: Optional[float] = None
    # D1: family_scope on which the headline numbers above are computed
    # (default ``"entry"``); the mixed-legacy values are exposed in parallel
    # so analysts can quantify the family-mix bias.
    family_scope: Optional[str] = None
    leader_pool_win_share_mixed: Optional[float] = None
    excess_return_pct_mixed: Optional[float] = None


class FiveLayerSummariesResponse(BaseModel):
    backtest_run_id: str
    items: List[FiveLayerGroupSummaryItem] = Field(default_factory=list)


class FiveLayerCalibrationItem(BaseModel):
    calibration_name: Optional[str] = None
    baseline_config_json: Optional[str] = None
    candidate_config_json: Optional[str] = None
    delta_metrics_json: Optional[str] = None
    decision: Optional[str] = None
    confidence: Optional[float] = None


class FiveLayerCalibrationResponse(BaseModel):
    backtest_run_id: str
    items: List[FiveLayerCalibrationItem] = Field(default_factory=list)


class FiveLayerRecommendationItem(BaseModel):
    recommendation_type: str
    target_scope: Optional[str] = None
    target_key: Optional[str] = None
    current_rule: Optional[str] = None
    suggested_change: Optional[str] = None
    recommendation_level: str
    sample_count: Optional[int] = None
    confidence: Optional[float] = None
    validation_status: Optional[str] = None
    evidence_json: Optional[str] = None


class FiveLayerRecommendationsResponse(BaseModel):
    backtest_run_id: str
    items: List[FiveLayerRecommendationItem] = Field(default_factory=list)


class FiveLayerFullPipelineResponse(BaseModel):
    run: FiveLayerRunResponse
    summaries: List[FiveLayerGroupSummaryItem] = Field(default_factory=list)
    recommendations: List[FiveLayerRecommendationItem] = Field(default_factory=list)
