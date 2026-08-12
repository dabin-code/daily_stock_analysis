from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd

from src.config import get_config

logger = logging.getLogger(__name__)


@dataclass
class ScreeningCandidateRecord:
    code: str
    name: str
    rank: int
    rule_score: float
    rule_hits: List[str]
    factor_snapshot: Dict[str, Any]
    matched_strategies: List[str] = field(default_factory=list)
    strategy_scores: Dict[str, float] = field(default_factory=dict)
    # 五层优先级排序会原地覆盖 rule_score，这里保留 screener 产出的原始质量分，
    # 供 Rank IC 等连续量分析使用。
    raw_rule_score: float = 0.0
    # ── 五层决策字段 (Phase 2A) ──
    setup_type: Optional[str] = None
    strategy_family: Optional[str] = None
    primary_strategy: Optional[str] = None
    contributing_strategies: List[str] = field(default_factory=list)
    trade_stage: Optional[str] = None
    entry_maturity: Optional[str] = None
    risk_level: Optional[str] = None
    market_regime: Optional[str] = None
    theme_position: Optional[str] = None
    candidate_pool_level: Optional[str] = None
    trade_plan_json: Optional[str] = None


@dataclass
class ScreeningEvaluationResult:
    selected: List[ScreeningCandidateRecord]
    rejected: List[Dict[str, Any]]


class ScreenerService:
    """规则首筛服务。

    统一走基于 YAML 策略元数据的 StrategyScreeningEngine 路径。
    """

    def __init__(
        self,
        min_list_days: Optional[int] = None,
        min_volume_ratio: Optional[float] = None,
        breakout_lookback_days: Optional[int] = None,
        skill_manager: Optional[Any] = None,
        strategy_names: Optional[List[str]] = None,
    ) -> None:
        if skill_manager is None:
            from src.agent.skills.base import SkillManager

            skill_manager = SkillManager()
            skill_manager.load_builtin_strategies()
        self._skill_manager = skill_manager
        self._strategy_names = strategy_names
        self.min_list_days = min_list_days if min_list_days is not None else _config_attr("screening_min_list_days", 120)
        self.min_volume_ratio = min_volume_ratio if min_volume_ratio is not None else _config_attr("screening_min_volume_ratio", 1.2)
        self.breakout_lookback_days = breakout_lookback_days if breakout_lookback_days is not None else _config_attr("screening_breakout_lookback_days", 20)

    def screen(
        self,
        snapshot_df: pd.DataFrame,
        candidate_limit: Optional[int] = None,
    ) -> List[ScreeningCandidateRecord]:
        """返回按得分排序后的候选列表。"""
        if candidate_limit is None:
            candidate_limit = _config_attr("screening_candidate_limit", 30)
        return self.evaluate(snapshot_df).selected[:candidate_limit]

    def evaluate(
        self,
        snapshot_df: pd.DataFrame,
        prefiltered_rules: Optional[List] = None,
    ) -> ScreeningEvaluationResult:
        """输出入选候选与淘汰原因。

        Parameters
        ----------
        snapshot_df : DataFrame
            因子快照。
        prefiltered_rules : list, optional
            外部预过滤的 StrategyScreeningRule 列表。提供时直接使用这些规则，
            跳过内部 skill_manager 查询——用于五层管线的策略事前过滤 (D5)。
        """
        if prefiltered_rules is not None:
            return self._evaluate_with_engine(snapshot_df, rules_override=prefiltered_rules)
        return self._evaluate_with_engine(snapshot_df)

    # ── Strategy engine path ─────────────────────────────────────────────

    def _evaluate_with_engine(
        self,
        snapshot_df: pd.DataFrame,
        rules_override: Optional[List] = None,
    ) -> ScreeningEvaluationResult:
        from src.services.strategy_screening_engine import (
            CommonFilterConfig,
            StrategyScreeningEngine,
            build_rules_from_skills,
        )

        if snapshot_df is None or snapshot_df.empty:
            return ScreeningEvaluationResult(selected=[], rejected=[])

        if rules_override is not None:
            rules = rules_override
        else:
            if self._skill_manager is None:
                raise RuntimeError("ScreenerService requires a skill_manager-backed strategy engine")
            skills = self._skill_manager.get_screening_rules(
                strategy_names=self._strategy_names,
            )
            if not skills:
                if self._strategy_names == []:
                    raise RuntimeError(
                        "No screening strategies found for strategy-engine path. Received empty strategy_names."
                    )
                requested = ", ".join(self._strategy_names or [])
                detail = f" Requested strategies: {requested}." if requested else ""
                raise RuntimeError(
                    f"No screening strategies found for strategy-engine path.{detail}"
                )
            rules = build_rules_from_skills(skills)
        engine = StrategyScreeningEngine()
        common = CommonFilterConfig(
            exclude_st=True,
            min_list_days=self.min_list_days,
        )
        result = engine.evaluate(
            snapshot_df=snapshot_df,
            rules=rules,
            common_filters=common,
        )

        selected = [
            ScreeningCandidateRecord(
                code=c.code,
                name=c.name,
                rank=c.rank,
                rule_score=c.final_score,
                rule_hits=c.rule_hits,
                factor_snapshot=c.factor_snapshot,
                matched_strategies=c.matched_strategies,
                strategy_scores=c.strategy_scores,
                setup_type=c.setup_type,
                strategy_family=c.strategy_family,
            )
            for c in result.selected
        ]
        return ScreeningEvaluationResult(selected=selected, rejected=result.rejected)

def _config_attr(attr: str, default: Any) -> Any:
    """Safely read a config attribute with fallback."""
    try:
        config = get_config()
        return getattr(config, attr, default)
    except Exception:
        return default
