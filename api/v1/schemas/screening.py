from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class CreateScreeningRunRequest(BaseModel):
    trade_date: Optional[date] = Field(None, description="交易日，默认当天")
    stock_codes: Optional[List[str]] = Field(None, description="可选的自定义股票池")
    mode: Optional[Literal["balanced", "aggressive", "quality"]] = Field(None, description="筛选模式预设，默认读取配置")
    candidate_limit: Optional[int] = Field(None, ge=1, le=200, description="规则候选上限，默认读取配置")
    ai_top_k: Optional[int] = Field(None, ge=0, le=50, description="AI 二筛上限，默认读取配置")
    strategies: Optional[List[str]] = Field(None, description="指定使用的筛选策略名称列表，默认使用全部可用策略")
    rerun_failed: bool = Field(False, description="是否在同日同模式失败任务存在时原地补跑")
    resume_from: Optional[Literal["ingesting", "factorizing"]] = Field(None, description="失败任务补跑起始阶段")
    market: Literal["cn"] = Field("cn", description="市场标识，MVP 仅支持 A 股")


class ScreeningBackfillToDateRequest(BaseModel):
    trade_date: date = Field(..., description="目标交易日，回填本地股票数据到该日")
    market: Literal["cn"] = Field("cn", description="市场标识，MVP 仅支持 A 股")


class ScreeningBackfillGovernanceResult(BaseModel):
    trade_date: Optional[str] = None
    run_result: Optional[str] = None
    pass_status: Optional[str] = None
    reason: Optional[str] = None


class ScreeningBackfillToDateResponse(BaseModel):
    status: str
    target_trade_date: str
    from_trade_date: Optional[str] = None
    backfilled_dates: List[str] = Field(default_factory=list)
    saved_rows: int = 0
    failed_dates: List[str] = Field(default_factory=list)
    governance_result: ScreeningBackfillGovernanceResult


class ScreeningNotifyRequest(BaseModel):
    limit: int = Field(10, ge=1, le=50, description="推送候选数量上限")
    with_ai_only: bool = Field(False, description="是否仅推送已进入 AI 二筛的候选")
    force: bool = Field(False, description="是否强制补发（仅对 failed/skipped 有效，v1 不允许重发 sent）")


# ── 五层决策上下文快照 ─────────────────────────────────────────────────────

class MarketEnvironmentSnapshot(BaseModel):
    """L1 大盘环境快照。"""
    market_regime: Optional[str] = None
    risk_level: Optional[str] = None
    index_name: str = "上证指数"
    index_price: Optional[float] = None
    index_ma100: Optional[float] = None
    is_safe: Optional[bool] = None
    message: Optional[str] = None


class SectorHeatSnapshotItem(BaseModel):
    """L2 单个板块热度快照。"""
    board_name: str
    board_type: str = "concept"
    sector_hot_score: float = 0.0
    sector_status: str = "cold"
    sector_stage: str = "ferment"
    canonical_theme: Optional[str] = None
    stock_count: int = 0
    up_count: int = 0
    limit_up_count: int = 0


class DecisionContextSnapshot(BaseModel):
    """L1/L2 决策上下文，附在 ScreeningRunResponse 中返回。"""
    market_environment: Optional[MarketEnvironmentSnapshot] = None
    sector_heat_results: List[SectorHeatSnapshotItem] = Field(default_factory=list)
    hot_theme_count: int = 0
    warm_theme_count: int = 0


class ThemePipelineThemeItem(BaseModel):
    name: str
    normalized_name: Optional[str] = None
    raw_name: Optional[str] = None
    raw_names: List[str] = Field(default_factory=list)
    source: Optional[str] = None
    priority_source: Optional[str] = None
    matched_sources: List[str] = Field(default_factory=list)
    heat_score: Optional[float] = None
    confidence: Optional[float] = None
    source_board: Optional[str] = None
    sector_status: Optional[str] = None
    sector_stage: Optional[str] = None
    stock_count: Optional[int] = None
    up_count: Optional[int] = None
    limit_up_count: Optional[int] = None
    catalyst_summary: Optional[str] = None
    keyword_count: Optional[int] = None
    keywords: List[str] = Field(default_factory=list)
    normalization_status: Optional[str] = None
    normalization_confidence: Optional[float] = None
    normalization_match_reasons: List[str] = Field(default_factory=list)
    normalization_matched_boards: List[str] = Field(default_factory=list)


class LocalThemePipelineSnapshot(BaseModel):
    source: str
    trade_date: Optional[str] = None
    market: Optional[str] = None
    hot_theme_count: int = 0
    warm_theme_count: int = 0
    selected_theme_names: List[str] = Field(default_factory=list)
    themes: List[ThemePipelineThemeItem] = Field(default_factory=list)


class ExternalThemePipelineSnapshot(BaseModel):
    source: str
    trade_date: Optional[str] = None
    market: Optional[str] = None
    accepted_theme_count: int = 0
    hot_theme_count: int = 0
    focus_theme_count: int = 0
    top_theme_names: List[str] = Field(default_factory=list)
    themes: List[ThemePipelineThemeItem] = Field(default_factory=list)


class FusedThemePipelineSnapshot(BaseModel):
    trade_date: Optional[str] = None
    market: Optional[str] = None
    active_sources: List[str] = Field(default_factory=list)
    selected_theme_names: List[str] = Field(default_factory=list)
    merged_theme_count: int = 0
    merged_themes: List[ThemePipelineThemeItem] = Field(default_factory=list)


class ScreeningRunResponse(BaseModel):
    run_id: str
    mode: Optional[str] = None
    status: str
    trade_date: Optional[str] = None
    market: Optional[str] = None
    universe_size: int = 0
    candidate_count: int = 0
    ai_top_k: int = 0
    error_summary: Optional[str] = None
    failed_symbols: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    sync_failure_ratio: float = 0.0
    config_snapshot: Dict[str, Any] = Field(default_factory=dict)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    # Notification lifecycle fields
    trigger_type: Optional[str] = "manual"
    notification_status: Optional[str] = None
    notification_attempts: int = 0
    notification_sent_at: Optional[str] = None
    notification_error: Optional[str] = None
    # 五层决策上下文
    strategy_names: Optional[List[str]] = None
    decision_context: Optional[DecisionContextSnapshot] = None
    local_theme_pipeline: Optional[LocalThemePipelineSnapshot] = None
    external_theme_pipeline: Optional[ExternalThemePipelineSnapshot] = None
    fused_theme_pipeline: Optional[FusedThemePipelineSnapshot] = None


class ScreeningRunListResponse(BaseModel):
    total: int
    items: List[ScreeningRunResponse]


class ScreeningCandidateItem(BaseModel):
    code: str
    name: Optional[str] = None
    rank: int
    rule_score: float
    selected_for_ai: bool
    matched_strategies: List[str] = Field(default_factory=list)
    strategy_scores: Dict[str, float] = Field(default_factory=dict)
    rule_hits: List[str] = Field(default_factory=list)
    factor_snapshot: Dict[str, Any] = Field(default_factory=dict)
    ai_query_id: Optional[str] = None
    ai_summary: Optional[str] = None
    ai_operation_advice: Optional[str] = None
    has_ai_analysis: Optional[bool] = None
    news_count: Optional[int] = None
    news_summary: Optional[str] = None
    recommendation_source: Optional[str] = None
    recommendation_reason: Optional[str] = None
    final_score: Optional[float] = None
    final_rank: Optional[int] = None
    # -- 五层系统新增字段 (Phase 1) --
    trade_stage: Optional[str] = None
    setup_type: Optional[str] = None
    strategy_family: Optional[str] = None
    entry_maturity: Optional[str] = None
    risk_level: Optional[str] = None
    market_regime: Optional[str] = None
    market_message: Optional[str] = None
    environment_ok: Optional[bool] = None
    index_price: Optional[float] = None
    index_ma100: Optional[float] = None
    theme_position: Optional[str] = None
    theme_tag: Optional[str] = None
    theme_score: Optional[float] = None
    sector_strength: Optional[float] = None
    theme_duration: Optional[str] = None
    trade_theme_stage: Optional[str] = None
    leader_stocks: List[str] = Field(default_factory=list)
    front_stocks: List[str] = Field(default_factory=list)
    candidate_pool_level: Optional[str] = None
    leader_score: Optional[float] = None
    relative_strength_market: Optional[float] = None
    relative_strength_sector: Optional[float] = None
    setup_freshness: Optional[float] = None
    setup_hit_reasons: List[str] = Field(default_factory=list)
    trade_plan: Optional[Dict[str, Any]] = None
    ai_review: Optional[Dict[str, Any]] = None
    # -- AI Review Protocol (Phase 3B-1) --
    ai_trade_stage: Optional[str] = None
    ai_reasoning: Optional[str] = None
    ai_confidence: Optional[float] = None
    ai_environment_ok: Optional[bool] = None
    ai_theme_alignment: Optional[bool] = None
    ai_entry_quality: Optional[str] = None
    stage_conflict: Optional[bool] = None
    result_source: Optional[str] = None
    is_fallback: Optional[bool] = None
    fallback_reason: Optional[str] = None
    downgrade_reasons: List[str] = Field(default_factory=list)
    initial_position: Optional[str] = None
    stop_loss_rule: Optional[str] = None
    take_profit_plan: Optional[str] = None
    invalidation_rule: Optional[str] = None
    prompt_version: Optional[str] = None
    model_name: Optional[str] = None
    parse_status: Optional[str] = None
    retry_count: Optional[int] = None


class ScreeningAnalysisHistoryRef(BaseModel):
    id: Optional[int] = None
    query_id: str
    stock_code: str
    stock_name: Optional[str] = None
    report_type: Optional[str] = None
    operation_advice: Optional[str] = None
    trend_prediction: Optional[str] = None
    sentiment_score: Optional[int] = None
    analysis_summary: Optional[str] = None
    created_at: Optional[str] = None


class ScreeningCandidateDetailResponse(ScreeningCandidateItem):
    analysis_history: Optional[ScreeningAnalysisHistoryRef] = None


class ScreeningCandidateListResponse(BaseModel):
    total: int
    items: List[ScreeningCandidateItem]


class ScreeningStrategyInfo(BaseModel):
    name: str
    display_name: str
    description: str
    category: str
    has_screening_rules: bool
    system_role: Optional[str] = None
    strategy_family: Optional[str] = None
    applicable_market: List[str] = Field(default_factory=list)
    applicable_theme: List[str] = Field(default_factory=list)
    setup_type: Optional[str] = None


class ScreeningStrategyListResponse(BaseModel):
    strategies: List[ScreeningStrategyInfo]
