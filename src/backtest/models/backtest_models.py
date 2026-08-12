# -*- coding: utf-8 -*-
"""Five-layer backtest ORM models.

Five tables:
  1. FiveLayerBacktestRun        – run metadata & version tracking
  2. FiveLayerBacktestEvaluation – candidate-level fact table (snapshot/replay dual-track)
  3. FiveLayerBacktestGroupSummary – aggregated statistics
  4. FiveLayerBacktestCalibrationOutput – parameter experiment results
  5. FiveLayerBacktestRecommendation – actionable suggestions with evidence
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict, Optional

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float, Integer, String, Text,
    Index, UniqueConstraint,
)

from src.storage import Base


# ── Table 1: Backtest Run ────────────────────────────────────────────────────

class FiveLayerBacktestRun(Base):
    """Top-level container for a single backtest execution."""
    __tablename__ = "five_layer_backtest_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    backtest_run_id = Column(String(64), unique=True, nullable=False, index=True)

    # Mode & model
    evaluation_mode = Column(String(32), nullable=False)  # historical_snapshot / rule_replay / parameter_calibration
    execution_model = Column(String(32), nullable=False)   # conservative / baseline / optimistic

    # Scope
    trade_date_from = Column(Date, nullable=False)
    trade_date_to = Column(Date, nullable=False)
    market = Column(String(16), nullable=False, default="cn")

    # Version tracking (snapshot integrity)
    data_version = Column(String(64))
    market_data_version = Column(String(64))
    theme_mapping_version = Column(String(64))
    candidate_snapshot_version = Column(String(64))
    rules_version = Column(String(64))

    # 代码版本与配置指纹。没有这两项，「同一份数据同一套代码」无法证明。
    # 注意：code_revision 仅作记录，**不参与可比性判定**——
    # spec 5.3 指出 git commit 会因无关改动而变化，用它当键会把
    # 本可对比的两次运行误判为不可比。可比性看 data_version 与 config_hash。
    code_revision = Column(String(64))
    config_hash = Column(String(64))

    # Config
    config_json = Column(Text)
    candidate_filter_json = Column(Text)

    # Status & counters
    status = Column(String(32), nullable=False, default="pending")
    sample_count = Column(Integer, default=0)
    completed_count = Column(Integer, default=0)
    error_count = Column(Integer, default=0)

    # Timestamps
    created_at = Column(DateTime, default=datetime.now)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)

    __table_args__ = (
        Index("ix_flbr_dates_market", "trade_date_from", "trade_date_to", "market"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "backtest_run_id": self.backtest_run_id,
            "evaluation_mode": self.evaluation_mode,
            "execution_model": self.execution_model,
            "trade_date_from": self.trade_date_from.isoformat() if self.trade_date_from else None,
            "trade_date_to": self.trade_date_to.isoformat() if self.trade_date_to else None,
            "market": self.market,
            "data_version": self.data_version,
            "market_data_version": self.market_data_version,
            "theme_mapping_version": self.theme_mapping_version,
            "candidate_snapshot_version": self.candidate_snapshot_version,
            "rules_version": self.rules_version,
            "code_revision": self.code_revision,
            "config_hash": self.config_hash,
            "config_json": self.config_json,
            "candidate_filter_json": self.candidate_filter_json,
            "status": self.status,
            "sample_count": self.sample_count,
            "completed_count": self.completed_count,
            "error_count": self.error_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


# ── Table 2: Evaluation (candidate-level fact table) ────────────────────────

class FiveLayerBacktestEvaluation(Base):
    """Candidate-level evaluation with snapshot/replayed dual-track fields."""
    __tablename__ = "five_layer_backtest_evaluations"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Run linkage
    backtest_run_id = Column(String(64), nullable=False, index=True)

    # Candidate linkage
    screening_run_id = Column(String(64), index=True)
    screening_candidate_id = Column(Integer, index=True)
    trade_date = Column(Date, index=True)
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(50))

    # ── Snapshot fields (decision-time values) ───────────────────────────
    snapshot_trade_stage = Column(String(32))
    snapshot_setup_type = Column(String(64))
    snapshot_entry_maturity = Column(String(16))
    snapshot_market_regime = Column(String(32))
    snapshot_theme_position = Column(String(32))
    snapshot_candidate_pool_level = Column(String(32))
    snapshot_risk_level = Column(String(16))

    # ── Replayed fields (rule-replay or calibration values) ──────────────
    replayed_trade_stage = Column(String(32))
    replayed_setup_type = Column(String(64))
    replayed_entry_maturity = Column(String(16))
    replayed_market_regime = Column(String(32))
    replayed_theme_position = Column(String(32))
    replayed_candidate_pool_level = Column(String(32))
    replayed_risk_level = Column(String(16))

    # ── Classification ───────────────────────────────────────────────────
    evaluation_mode = Column(String(32))
    signal_family = Column(String(16), nullable=False, index=True)  # entry / exit / observation
    signal_type = Column(String(32))
    evaluator_type = Column(String(16), nullable=False)
    execution_model = Column(String(32))
    snapshot_source = Column(String(32))  # screening_candidate / replayed / calibrated
    known_at_decision_time = Column(Boolean, default=True)
    replayed = Column(Boolean, default=False)

    # ── Execution result fields ──────────────────────────────────────────
    entry_fill_status = Column(String(32))
    entry_fill_price = Column(Float)
    exit_fill_status = Column(String(32))
    exit_fill_price = Column(Float)
    planned_entry_price = Column(Float)
    planned_stop_loss_price = Column(Float)
    planned_take_profit_price = Column(Float)
    actual_entry_price = Column(Float)
    actual_entry_date = Column(Date)
    actual_exit_price = Column(Float)
    actual_exit_date = Column(Date)
    exit_reason = Column(String(32))
    trade_return_pct = Column(Float)
    trade_replay_status = Column(String(32), index=True)
    limit_blocked = Column(Boolean, default=False)
    gap_adjusted = Column(Boolean, default=False)
    ambiguous_intraday_order = Column(Boolean, default=False)

    # ── Metric fields ────────────────────────────────────────────────────
    forward_return_1d = Column(Float)
    forward_return_3d = Column(Float)
    forward_return_5d = Column(Float)
    forward_return_10d = Column(Float)
    mae = Column(Float)  # max adverse excursion
    mfe = Column(Float)  # max favorable excursion
    max_drawdown_from_peak = Column(Float)
    optimal_entry_deviation = Column(Float)
    optimal_entry_timing = Column(Integer)
    holding_days = Column(Integer)
    plan_success = Column(Boolean)
    signal_quality_score = Column(Float)
    risk_avoided_pct = Column(Float)
    opportunity_cost_pct = Column(Float)

    # ── Labels & outcome ─────────────────────────────────────────────────
    outcome = Column(String(16))
    stage_success = Column(Boolean)
    eval_status = Column(String(16), default="pending")  # pending / evaluated / suppressed / error
    # Why this row was not aggregatable (or why evaluation failed). Codes:
    #   exec_not_filled:<fill_status>  – execution model rejected the entry
    #   no_forward_bars                – missing forward OHLCV bars
    #   invalid_hypothetical_price     – observation hypothetical price <= 0
    #   exception:<ExceptionClass>     – evaluation raised an unexpected error
    #   missing_forward_return_5d      – legacy fallback for old rows
    #   missing_risk_avoided_pct       – legacy fallback for old rows
    #   missing_primary_metric         – legacy fallback (unknown family)
    suppression_reason = Column(String(64), index=True)
    evaluated_at = Column(DateTime)

    # ── JSON extension fields ────────────────────────────────────────────
    metrics_json = Column(Text)
    evidence_json = Column(Text)
    trade_plan_json = Column(Text)
    factor_snapshot_json = Column(Text)

    __table_args__ = (
        UniqueConstraint("backtest_run_id", "screening_candidate_id", name="uix_flbe_run_candidate"),
        Index("ix_flbe_trade_date_code", "trade_date", "code"),
        Index("ix_flbe_signal_family", "backtest_run_id", "signal_family"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "backtest_run_id": self.backtest_run_id,
            "screening_run_id": self.screening_run_id,
            "screening_candidate_id": self.screening_candidate_id,
            "trade_date": self.trade_date.isoformat() if self.trade_date else None,
            "code": self.code,
            "name": self.name,
            "snapshot_trade_stage": self.snapshot_trade_stage,
            "snapshot_setup_type": self.snapshot_setup_type,
            "snapshot_entry_maturity": self.snapshot_entry_maturity,
            "snapshot_market_regime": self.snapshot_market_regime,
            "snapshot_theme_position": self.snapshot_theme_position,
            "snapshot_candidate_pool_level": self.snapshot_candidate_pool_level,
            "snapshot_risk_level": self.snapshot_risk_level,
            "replayed_trade_stage": self.replayed_trade_stage,
            "replayed_setup_type": self.replayed_setup_type,
            "replayed_entry_maturity": self.replayed_entry_maturity,
            "replayed_market_regime": self.replayed_market_regime,
            "replayed_theme_position": self.replayed_theme_position,
            "replayed_candidate_pool_level": self.replayed_candidate_pool_level,
            "replayed_risk_level": self.replayed_risk_level,
            "evaluation_mode": self.evaluation_mode,
            "signal_family": self.signal_family,
            "signal_type": self.signal_type,
            "evaluator_type": self.evaluator_type,
            "execution_model": self.execution_model,
            "snapshot_source": self.snapshot_source,
            "replayed": self.replayed,
            "entry_fill_status": self.entry_fill_status,
            "entry_fill_price": self.entry_fill_price,
            "exit_fill_status": self.exit_fill_status,
            "exit_fill_price": self.exit_fill_price,
            "planned_entry_price": self.planned_entry_price,
            "planned_stop_loss_price": self.planned_stop_loss_price,
            "planned_take_profit_price": self.planned_take_profit_price,
            "actual_entry_price": self.actual_entry_price,
            "actual_entry_date": self.actual_entry_date.isoformat() if self.actual_entry_date else None,
            "actual_exit_price": self.actual_exit_price,
            "actual_exit_date": self.actual_exit_date.isoformat() if self.actual_exit_date else None,
            "exit_reason": self.exit_reason,
            "trade_return_pct": self.trade_return_pct,
            "trade_replay_status": self.trade_replay_status,
            "limit_blocked": self.limit_blocked,
            "gap_adjusted": self.gap_adjusted,
            "forward_return_1d": self.forward_return_1d,
            "forward_return_3d": self.forward_return_3d,
            "forward_return_5d": self.forward_return_5d,
            "forward_return_10d": self.forward_return_10d,
            "mae": self.mae,
            "mfe": self.mfe,
            "max_drawdown_from_peak": self.max_drawdown_from_peak,
            "holding_days": self.holding_days,
            "plan_success": self.plan_success,
            "signal_quality_score": self.signal_quality_score,
            "risk_avoided_pct": self.risk_avoided_pct,
            "opportunity_cost_pct": self.opportunity_cost_pct,
            "outcome": self.outcome,
            "stage_success": self.stage_success,
            "eval_status": self.eval_status,
            "suppression_reason": self.suppression_reason,
            "evaluated_at": self.evaluated_at.isoformat() if self.evaluated_at else None,
            "metrics_json": self.metrics_json,
            "evidence_json": self.evidence_json,
            "trade_plan_json": self.trade_plan_json,
            "factor_snapshot_json": self.factor_snapshot_json,
        }


# ── Table 3: Group Summary ──────────────────────────────────────────────────

class FiveLayerBacktestGroupSummary(Base):
    """Aggregated backtest stats per group_type/group_key."""
    __tablename__ = "five_layer_backtest_group_summaries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    backtest_run_id = Column(String(64), nullable=False, index=True)

    group_type = Column(String(64), nullable=False)  # overall / signal_family / setup_type / market_regime / combo / strategy_cohort
    group_key = Column(String(128), nullable=False)    # "entry" / "trend_breakout" / "snapshot_theme_position=leader|snapshot_setup_type=trend_breakout" / "ps=trend_pullback|sb=boundary|mr=balanced|cp=tier2|em=medium"

    sample_count = Column(Integer, default=0)
    avg_return_pct = Column(Float)
    median_return_pct = Column(Float)
    win_rate_pct = Column(Float)
    avg_mae = Column(Float)
    avg_mfe = Column(Float)
    avg_drawdown = Column(Float)
    # ``top_k_hit_rate`` is a DEPRECATED alias preserved for old API consumers
    # and dashboards. Always written together with ``leader_pool_win_share``
    # by the aggregator. New code should read ``leader_pool_win_share``.
    top_k_hit_rate = Column(Float)
    leader_pool_win_share = Column(Float)
    excess_return_pct = Column(Float)
    ranking_consistency = Column(Float)
    p25_return_pct = Column(Float)
    p75_return_pct = Column(Float)
    extreme_sample_ratio = Column(Float)
    time_bucket_stability = Column(Float)
    profit_factor = Column(Float)
    avg_holding_days = Column(Float)
    max_consecutive_losses = Column(Integer)
    plan_execution_rate = Column(Float)
    stage_accuracy_rate = Column(Float)
    system_grade = Column(String(4))

    metrics_json = Column(Text)

    computed_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("backtest_run_id", "group_type", "group_key", name="uix_flbgs_run_group"),
        Index("ix_flbgs_group", "group_type", "group_key"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "backtest_run_id": self.backtest_run_id,
            "group_type": self.group_type,
            "group_key": self.group_key,
            "sample_count": self.sample_count,
            "avg_return_pct": self.avg_return_pct,
            "median_return_pct": self.median_return_pct,
            "win_rate_pct": self.win_rate_pct,
            "avg_mae": self.avg_mae,
            "avg_mfe": self.avg_mfe,
            "avg_drawdown": self.avg_drawdown,
            "top_k_hit_rate": self.top_k_hit_rate,
            "leader_pool_win_share": self.leader_pool_win_share,
            "excess_return_pct": self.excess_return_pct,
            "ranking_consistency": self.ranking_consistency,
            "p25_return_pct": self.p25_return_pct,
            "p75_return_pct": self.p75_return_pct,
            "extreme_sample_ratio": self.extreme_sample_ratio,
            "time_bucket_stability": self.time_bucket_stability,
            "profit_factor": self.profit_factor,
            "avg_holding_days": self.avg_holding_days,
            "max_consecutive_losses": self.max_consecutive_losses,
            "plan_execution_rate": self.plan_execution_rate,
            "stage_accuracy_rate": self.stage_accuracy_rate,
            "system_grade": self.system_grade,
            "metrics_json": self.metrics_json,
        }


# ── Table 4: Calibration Output ─────────────────────────────────────────────

class FiveLayerBacktestCalibrationOutput(Base):
    """Parameter experiment comparison results."""
    __tablename__ = "five_layer_backtest_calibration_outputs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    backtest_run_id = Column(String(64), nullable=False, index=True)

    calibration_name = Column(String(128))
    baseline_config_json = Column(Text)
    candidate_config_json = Column(Text)
    delta_metrics_json = Column(Text)
    decision = Column(String(32))  # accept / reject / inconclusive
    confidence = Column(Float)

    created_at = Column(DateTime, default=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "backtest_run_id": self.backtest_run_id,
            "calibration_name": self.calibration_name,
            "baseline_config_json": self.baseline_config_json,
            "candidate_config_json": self.candidate_config_json,
            "delta_metrics_json": self.delta_metrics_json,
            "decision": self.decision,
            "confidence": self.confidence,
        }


# ── Table 5: Recommendation ─────────────────────────────────────────────────

class FiveLayerBacktestRecommendation(Base):
    """Structured recommendation with evidence chain."""
    __tablename__ = "five_layer_backtest_recommendations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    backtest_run_id = Column(String(64), nullable=False, index=True)

    recommendation_type = Column(String(64), nullable=False)  # threshold_adjustment / rule_change / ...
    target_scope = Column(String(64))    # setup_type / market_regime / combo
    target_key = Column(String(128))     # trend_breakout / balanced+entry

    current_rule = Column(Text)
    suggested_change = Column(Text)

    recommendation_level = Column(String(16), nullable=False)  # observation / hypothesis / actionable
    sample_count = Column(Integer)
    confidence = Column(Float)
    validation_status = Column(String(32))  # pending / confirmed / rejected

    evidence_json = Column(Text)
    metrics_before_json = Column(Text)
    metrics_after_json = Column(Text)

    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index("ix_flbr_level_scope", "recommendation_level", "target_scope"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "backtest_run_id": self.backtest_run_id,
            "recommendation_type": self.recommendation_type,
            "target_scope": self.target_scope,
            "target_key": self.target_key,
            "current_rule": self.current_rule,
            "suggested_change": self.suggested_change,
            "recommendation_level": self.recommendation_level,
            "sample_count": self.sample_count,
            "confidence": self.confidence,
            "validation_status": self.validation_status,
            "evidence_json": self.evidence_json,
        }
