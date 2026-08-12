# -*- coding: utf-8 -*-
"""
FiveLayerPipeline — L2 前置过滤 + 策略事前控制 + L3/L4/L5 逐票否决。

核心流程:
  L1 (Phase 1 已前置于 execute_run) →
  L2 板块热度 + 主线识别 → 缩小 universe →
  策略前置过滤 (D5) → 选股 →
  L3/L4/L5 逐票裁决（否决权）→
  五层优先级排序

D1 修复: 五层从"后置标注"翻转为"前置过滤 + 逐票否决"。
D3 修复: 先识别主线题材，再匹配股票（由 ThemePositionResolver 实现）。
D5 修复: 选股前过滤策略（由 StrategyDispatcher.get_allowed_rules 实现）。
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Set

import pandas as pd

from src.schemas.trading_types import (
    MarketEnvironment,
    SetupType,
    TradeStage,
)
from src.services._debug_session_logger import write_debug_log
from src.services.bottom_divergence_v2_trade_support import (
    is_v2_execution_allowed,
    resolve_current_stage_buy_point,
)
from src.services.screener_service import ScreeningCandidateRecord, ScreenerService
from src.services.setup_stop_loss import resolve_setup_stop_loss

logger = logging.getLogger(__name__)


def _counter_to_dict(values: List[str]) -> Dict[str, int]:
    counter = Counter(value for value in values if value)
    return dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))


def _rejection_reason_counts(rejected: List[Dict[str, Any]]) -> Dict[str, int]:
    counter: Counter[str] = Counter()
    for item in rejected:
        for reason in item.get("rejection_reasons", []) or []:
            if reason:
                counter[str(reason)] += 1
    return dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))


def _top_items(summary: Dict[str, int], limit: int = 8) -> Dict[str, int]:
    items = list(summary.items())[:limit]
    return dict(items)


def _limit_list(values: List[str], limit: int = 12) -> List[str]:
    return values[:limit]


def _resolve_effective_scores(factor_snapshot: Dict[str, Any]) -> tuple[float, float, str]:
    theme_leader_score = float(factor_snapshot.get("theme_leader_score", 0.0) or 0.0)
    base_leader_score = float(
        factor_snapshot.get("base_leader_score", factor_snapshot.get("leader_score", 0.0)) or 0.0
    )
    theme_extreme_strength = float(factor_snapshot.get("theme_extreme_strength_score", 0.0) or 0.0)
    base_extreme_strength = float(
        factor_snapshot.get(
            "base_extreme_strength_score",
            factor_snapshot.get("extreme_strength_score", 0.0),
        )
        or 0.0
    )

    effective_leader_score = theme_leader_score if theme_leader_score > 0.0 else base_leader_score
    effective_extreme_strength = (
        theme_extreme_strength if theme_extreme_strength > 0.0 else base_extreme_strength
    )
    score_source = "theme" if theme_leader_score > 0.0 else "base"
    return effective_leader_score, effective_extreme_strength, score_source


def _normalize_candidate_record(item: Any) -> ScreeningCandidateRecord:
    if isinstance(item, ScreeningCandidateRecord):
        return item
    if isinstance(item, dict):
        return ScreeningCandidateRecord(
            code=str(item.get("code", "")),
            name=str(item.get("name", "") or ""),
            rank=int(item.get("rank", 0) or 0),
            rule_score=float(item.get("rule_score", 0.0) or 0.0),
            rule_hits=list(item.get("rule_hits", []) or []),
            factor_snapshot=dict(item.get("factor_snapshot", {}) or {}),
            matched_strategies=list(item.get("matched_strategies", []) or []),
            strategy_scores=dict(item.get("strategy_scores", {}) or {}),
            setup_type=item.get("setup_type"),
            strategy_family=item.get("strategy_family"),
            primary_strategy=item.get("primary_strategy"),
            contributing_strategies=list(item.get("contributing_strategies", []) or []),
        )
    raise TypeError(f"Unsupported candidate type for five-layer pipeline: {type(item)!r}")


# ── 五层优先级排序权重 ──────────────────────────────────────────────────────
_STAGE_PRIORITY: Dict[str, int] = {
    "add_on_strength": 100,
    "probe_entry": 80,
    "focus": 40,
    "watch": 10,
    "stand_aside": 0,
    "reject": -50,
}

_POOL_PRIORITY: Dict[str, int] = {
    "leader_pool": 30,
    "focus_list": 15,
    "watchlist": 0,
}

_THEME_PRIORITY: Dict[str, int] = {
    "main_theme": 20,
    "secondary_theme": 10,
    "follower_theme": 0,
    "fading_theme": -10,
    "non_theme": -20,
}

# L2 universe 缩小: 主线板块候选不足时的最低阈值
MIN_THEME_CANDIDATES = 10


@dataclass
class PipelineResult:
    """FiveLayerPipeline 输出。"""
    candidates: List[ScreeningCandidateRecord]
    decision_context: Dict[str, Any]
    market_env: MarketEnvironment
    pipeline_stats: Dict[str, Any] = field(default_factory=dict)


class FiveLayerPipeline:
    """L2 前置过滤 → 策略前置控制 → 选股 → L3/L4/L5 逐票否决。"""

    def run(
        self,
        snapshot_df: pd.DataFrame,
        trade_date: date,
        market_env: MarketEnvironment,
        guard_result: Any,
        screener_service: ScreenerService,
        candidate_limit: int,
        db_manager: Any,
        skill_manager: Optional[Any] = None,
        lock_universe: bool = False,
    ) -> PipelineResult:
        stats: Dict[str, Any] = {
            "universe_before": len(snapshot_df),
        }
        run_id = getattr(getattr(screener_service, "_context", None), "run_id", None)
        if not run_id:
            run_id = f"{trade_date.isoformat()}-{market_env.regime.value}"

        hot_theme_stock_count = 0
        theme_match_passed_count = 0
        leader_score_source_counts: Dict[str, int] = {}
        if snapshot_df is not None and not snapshot_df.empty:
            if "is_hot_theme_stock" in snapshot_df.columns:
                hot_theme_stock_count = int(snapshot_df["is_hot_theme_stock"].fillna(False).sum())
            if "theme_match_score" in snapshot_df.columns:
                theme_match_passed_count = int(
                    (snapshot_df["theme_match_score"].fillna(0.0).astype(float) >= 0.8).sum()
                )
            if "leader_score_source" in snapshot_df.columns:
                source_values = snapshot_df["leader_score_source"].fillna("missing").astype(str).tolist()
                leader_score_source_counts = {
                    source: source_values.count(source)
                    for source in sorted(set(source_values))
                }
        # ── L2: 板块热度计算 ──────────────────────────────────────────
        from src.services.sector_heat_engine import SectorHeatEngine
        from src.services.theme_aggregation_service import ThemeAggregationService
        from src.services.theme_position_resolver import ThemePositionResolver
        from src.services.theme_mapping_registry import ThemeMappingRegistry

        sector_results = []
        all_sector_results = []
        try:
            data_version = getattr(
                db_manager,
                "validation_data_version",
                None,
            )
            cache_key = (
                (str(data_version), trade_date)
                if data_version is not None
                else None
            )
            validation_cache = getattr(
                self,
                "_validation_sector_cache",
                {},
            )
            if cache_key is not None and cache_key in validation_cache:
                all_sector_results = validation_cache[cache_key]
            else:
                sector_engine = SectorHeatEngine(db_manager=db_manager)
                all_sector_results = sector_engine.compute_all_sectors(
                    snapshot_df,
                    trade_date,
                )
                if cache_key is not None:
                    validation_cache[cache_key] = all_sector_results
                    self._validation_sector_cache = validation_cache
            sector_results = [
                s for s in all_sector_results
                if s.sector_status in ("hot", "warm")
            ]
        except Exception as exc:
            logger.warning("pipeline L2 SectorHeatEngine failed (degraded): %s", exc)

        stats["total_sectors"] = len(all_sector_results)
        stats["hot_warm_sectors"] = len(sector_results)
        stats["hot_sector_count"] = sum(1 for s in all_sector_results if s.sector_status == "hot")
        stats["warm_sector_count"] = sum(1 for s in all_sector_results if s.sector_status == "warm")
        stats["sector_status_counts"] = _counter_to_dict(
            [str(s.sector_status) for s in all_sector_results]
        )
        logger.info(
            "pipeline L2 sectors: total=%d hot_warm=%d status_counts=%s",
            len(all_sector_results),
            len(sector_results),
            stats["sector_status_counts"],
        )
        # ── L2: 题材聚合 + 主线识别 ──────────────────────────────────
        theme_registry = None
        try:
            theme_registry = ThemeMappingRegistry()
            if theme_registry.is_empty:
                logger.warning("pipeline: ThemeMappingRegistry loaded 0 mappings")
        except Exception as exc:
            logger.warning("pipeline ThemeMappingRegistry failed: %s", exc)

        theme_results = []
        try:
            agg_service = ThemeAggregationService(registry=theme_registry)
            theme_results = agg_service.aggregate(sector_results)
        except Exception as exc:
            logger.warning("pipeline L2 ThemeAggregation failed: %s", exc)
            theme_registry = None

        theme_resolver = ThemePositionResolver(
            sector_results, theme_results, None, registry=theme_registry,
        )
        identified_theme_positions = _counter_to_dict(
            [theme.position.value for theme in theme_resolver.identified_themes]
        )
        stats["theme_result_count"] = len(theme_results)
        stats["identified_theme_position_counts"] = identified_theme_positions
        stats["top_hot_boards"] = [
            {
                "board_name": str(s.board_name),
                "board_strength_score": round(float(getattr(s, "board_strength_score", s.sector_hot_score)), 2),
                "board_strength_rank": int(getattr(s, "board_strength_rank", 0) or 0),
                "stage": str(s.sector_stage),
            }
            for s in sorted(
                [item for item in all_sector_results if item.sector_status == "hot"],
                key=lambda item: float(getattr(item, "board_strength_score", item.sector_hot_score)),
                reverse=True,
            )[:8]
        ]
        stats["top_warm_boards"] = [
            {
                "board_name": str(s.board_name),
                "board_strength_score": round(float(getattr(s, "board_strength_score", s.sector_hot_score)), 2),
                "board_strength_rank": int(getattr(s, "board_strength_rank", 0) or 0),
                "stage": str(s.sector_stage),
            }
            for s in sorted(
                [item for item in all_sector_results if item.sector_status == "warm"],
                key=lambda item: float(getattr(item, "board_strength_score", item.sector_hot_score)),
                reverse=True,
            )[:8]
        ]
        stats["board_strength_rank_preview"] = [
            {
                "board_name": str(s.board_name),
                "sector_status": str(s.sector_status),
                "board_strength_score": round(float(getattr(s, "board_strength_score", s.sector_hot_score)), 2),
                "board_strength_rank": int(getattr(s, "board_strength_rank", 0) or 0),
            }
            for s in sorted(
                all_sector_results,
                key=lambda item: int(getattr(item, "board_strength_rank", 9999) or 9999),
            )[:8]
        ]

        # ── L2: Universe 缩小（仅主线/次线板块的成员） ────────────────
        main_theme_boards = theme_resolver.get_main_theme_boards()
        stats["main_theme_board_count"] = len(main_theme_boards)
        theme_universe_df = snapshot_df
        l2_filter_mode = "full_universe"
        theme_member_candidate_count = 0

        if lock_universe:
            # 题材选股已在 universe 层锁定为指定板块成分股，
            # 此处跳过热点板块收窄，让全部成分股进入策略筛选。
            l2_filter_mode = "theme_universe_locked"
            logger.info(
                "pipeline L2: universe locked to provided boards (%d stocks), skip theme shrink",
                len(snapshot_df),
            )
        elif main_theme_boards:
            member_codes = self._get_theme_member_codes(db_manager, main_theme_boards)
            if member_codes:
                mask = snapshot_df["code"].isin(member_codes)
                theme_filtered = snapshot_df[mask].copy()
                theme_member_candidate_count = len(theme_filtered)

                if len(theme_filtered) >= MIN_THEME_CANDIDATES:
                    theme_universe_df = theme_filtered
                    l2_filter_mode = "theme_shrink"
                    logger.info(
                        "pipeline L2 universe shrink: %d → %d (boards=%d)",
                        len(snapshot_df), len(theme_universe_df), len(main_theme_boards),
                    )
                else:
                    l2_filter_mode = "theme_fallback_insufficient_candidates"
                    logger.info(
                        "pipeline L2: theme candidates=%d < min=%d, using full universe",
                        len(theme_filtered), MIN_THEME_CANDIDATES,
                    )
            else:
                l2_filter_mode = "theme_fallback_no_members"

        stats["universe_after_l2"] = len(theme_universe_df)
        stats["l2_filter_mode"] = l2_filter_mode
        stats["theme_member_candidate_count"] = theme_member_candidate_count
        logger.info(
            "pipeline L2 themes: aggregated=%d identified=%s main_theme_boards=%d",
            len(theme_results),
            identified_theme_positions,
            len(main_theme_boards),
        )
        # region agent log
        write_debug_log(
            location="src/services/five_layer_pipeline.py:run",
            message="Five-layer pipeline L2 summary",
            hypothesis_id="H2,H3,H4",
            run_id=run_id,
            data={
                "snapshot_rows": int(len(snapshot_df)),
                "hot_theme_stock_count": int(hot_theme_stock_count),
                "theme_match_passed_count": int(theme_match_passed_count),
                "leader_score_source_counts": leader_score_source_counts,
                "total_sectors": int(len(all_sector_results)),
                "hot_warm_sector_count": int(len(sector_results)),
                "hot_sector_count": int(stats["hot_sector_count"]),
                "warm_sector_count": int(stats["warm_sector_count"]),
                "sector_status_counts": stats["sector_status_counts"],
                "theme_result_count": int(len(theme_results)),
                "identified_theme_position_counts": identified_theme_positions,
                "main_theme_board_count": int(len(main_theme_boards)),
                "main_theme_boards": _limit_list(sorted(list(main_theme_boards)), limit=12),
                "l2_filter_mode": l2_filter_mode,
                "theme_member_candidate_count": int(theme_member_candidate_count),
                "universe_after_l2": int(len(theme_universe_df)),
                "top_hot_boards": stats["top_hot_boards"],
                "top_warm_boards": stats["top_warm_boards"],
                "board_strength_rank_preview": stats["board_strength_rank_preview"],
            },
        )
        # endregion
        logger.info(
            "pipeline L2 universe: before=%d after=%d mode=%s theme_member_candidates=%d",
            len(snapshot_df),
            len(theme_universe_df),
            l2_filter_mode,
            theme_member_candidate_count,
        )

        # ── 策略前置过滤 (D5) ─────────────────────────────────────────
        prefiltered_rules = None
        all_rules = None
        allowed_rule_names: List[str] = []
        blocked_rule_names: List[str] = []
        stats["total_rules"] = 0
        stats["allowed_rules"] = 0
        stats["allowed_rule_names"] = []
        stats["blocked_rule_names"] = []

        if skill_manager is not None:
            try:
                from src.services.strategy_screening_engine import build_rules_from_skills
                from src.services.strategy_dispatcher import StrategyDispatcher

                skills = skill_manager.get_screening_rules()
                if skills:
                    all_rules = build_rules_from_skills(skills)
                    dispatcher = StrategyDispatcher(all_rules)
                    prefiltered_rules = dispatcher.get_allowed_rules(
                        all_rules, market_env.regime,
                    )
                    stats["total_rules"] = len(all_rules)
                    stats["allowed_rules"] = len(prefiltered_rules)
                    allowed_rule_names = [rule.strategy_name for rule in prefiltered_rules]
                    allowed_name_set = set(allowed_rule_names)
                    blocked_rule_names = [
                        rule.strategy_name for rule in all_rules
                        if rule.strategy_name not in allowed_name_set
                    ]
                    stats["allowed_rule_names"] = allowed_rule_names
                    stats["blocked_rule_names"] = blocked_rule_names
                    logger.info(
                        "pipeline D5: regime=%s total_rules=%d allowed=%d blocked=%d allowed_names=%s blocked_names=%s",
                        market_env.regime.value,
                        len(all_rules),
                        len(prefiltered_rules),
                        len(blocked_rule_names),
                        _limit_list(allowed_rule_names),
                        _limit_list(blocked_rule_names),
                    )
            except Exception as exc:
                logger.warning("pipeline D5 strategy pre-filter failed: %s", exc)
        else:
            logger.info("pipeline D5: skipped pre-filter because skill_manager is unavailable")
        # ── 选股（在缩小的 universe + 过滤后的策略上执行）─────────────
        evaluation = screener_service.evaluate(
            theme_universe_df,
            prefiltered_rules=prefiltered_rules,
        )
        selected = [_normalize_candidate_record(item) for item in evaluation.selected[:candidate_limit]]
        stats["matched_before_limit"] = len(evaluation.selected)
        stats["selected_after_limit"] = len(selected)
        stats["rejected_before_l345"] = len(evaluation.rejected)
        stats["screening_rejection_reason_counts"] = _rejection_reason_counts(evaluation.rejected)
        logger.info(
            "pipeline screening: universe=%d matched_before_limit=%d selected_after_limit=%d rejected=%d top_reasons=%s",
            len(theme_universe_df),
            len(evaluation.selected),
            len(selected),
            len(evaluation.rejected),
            _top_items(stats["screening_rejection_reason_counts"]),
        )
        if not selected:
            stats.setdefault("vetoed_count", 0)
            stats.setdefault("kept_count", 0)
            decision_context = self._build_decision_context(
                market_env, guard_result, sector_results, theme_registry,
            )
            stats.setdefault("trade_stage_counts", {})
            stats.setdefault("theme_position_counts", {})
            stats.setdefault("setup_type_counts", {})
            stats.setdefault("candidate_pool_level_counts", {})
            stats.setdefault("vetoed_stage_counts", {})
            decision_context["pipeline_stats"] = stats
            return PipelineResult(
                candidates=[],
                decision_context=decision_context,
                market_env=market_env,
                pipeline_stats=stats,
            )

        # ── L3/L4/L5 逐票裁决 ────────────────────────────────────────
        from src.services.candidate_pool_classifier import CandidatePoolClassifier
        from src.services.setup_resolver import SetupResolver
        from src.services.entry_maturity_assessor import EntryMaturityAssessor
        from src.services.setup_freshness_assessor import SetupFreshnessAssessor
        from src.services.trade_stage_judge import TradeStageJudge
        from src.services.strategy_dispatcher import StrategyDispatcher
        from src.services.trade_plan_builder import TradePlanBuilder

        all_codes = [c.code for c in selected]
        board_map = db_manager.batch_get_instrument_board_names(all_codes)

        pool_classifier = CandidatePoolClassifier()
        maturity_assessor = EntryMaturityAssessor()
        freshness_assessor = SetupFreshnessAssessor()
        stage_judge = TradeStageJudge()
        plan_builder = TradePlanBuilder()

        dispatcher = None
        setup_resolver = None
        if all_rules:
            try:
                dispatcher = StrategyDispatcher(all_rules)
                setup_resolver = SetupResolver(all_rules)
            except Exception as exc:
                logger.warning("pipeline: dispatcher/resolver init failed: %s", exc)

        vetoed: List[ScreeningCandidateRecord] = []
        kept: List[ScreeningCandidateRecord] = []

        for candidate in selected:
            fs = candidate.factor_snapshot or {}
            stock_boards = board_map.get(candidate.code, [])

            # L2: 题材地位
            theme_decision = theme_resolver.resolve(stock_boards)
            tp = theme_decision.theme_position

            # Phase 2B: 策略调度 + 买点收敛
            if dispatcher is not None and setup_resolver is not None:
                dispatch_result = dispatcher.filter_strategies(
                    candidate.matched_strategies or [], market_env.regime,
                )
                resolution = setup_resolver.resolve(
                    allowed_strategies=dispatch_result.allowed_strategies,
                    strategy_scores=candidate.strategy_scores or {},
                    market_regime=market_env.regime,
                    theme_position=tp,
                    factor_snapshot=fs,
                )
                st = resolution.setup_type
                candidate.setup_type = st.value if st != SetupType.NONE else None
                candidate.strategy_family = (
                    resolution.strategy_family.value if resolution.strategy_family else None
                )
                candidate.primary_strategy = resolution.primary_strategy
                candidate.contributing_strategies = [
                    strategy
                    for strategy in (resolution.contributing_strategies or [])
                    if strategy != resolution.primary_strategy
                ]
                candidate.matched_strategies = dispatch_result.allowed_strategies
            else:
                try:
                    st = SetupType(candidate.setup_type) if candidate.setup_type else SetupType.NONE
                except ValueError:
                    st = SetupType.NONE
                candidate.primary_strategy = None
                candidate.contributing_strategies = []

            # L4: 买点成熟度
            entry_mat = maturity_assessor.assess(st, fs)
            setup_freshness = freshness_assessor.assess(st, fs)

            # L3: 候选池分级
            leader_score, extreme_strength, leader_score_source = _resolve_effective_scores(fs)
            pool_level = pool_classifier.classify(
                leader_score=leader_score,
                extreme_strength_score=extreme_strength,
                theme_position=tp,
                market_regime=market_env.regime,
                is_limit_up=bool(fs.get("is_limit_up", False)),
                # A3/C：把"已走远勿追"阶段标签交给 L3 做 opt-in 降级；
                # snapshot 没有 stage_label 时传 None，完全保留旧行为。
                stage_label=fs.get("stage_label") or None,
            )

            # L5: 交易阶段裁决
            v2_execution_allowed = (
                is_v2_execution_allowed(fs)
                if st == SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY
                else True
            )
            # 止损锚点与 TradePlanBuilder 共用同一解析实现：解析不出止损
            # 才把候选压在 focus。曾经这里读的 has_stop_loss 键从未被写入，
            # 于是所有非 v2 setup 恒为 False，交易计划恒为空。
            resolved_stop = resolve_setup_stop_loss(st, fs)
            has_stop = (
                (
                    v2_execution_allowed
                    and resolve_current_stage_buy_point(fs) is not None
                    and resolved_stop is not None
                )
                if st == SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY
                else resolved_stop is not None
            )
            trade_stage = stage_judge.judge(
                env=market_env,
                setup_type=st,
                entry_maturity=entry_mat,
                pool_level=pool_level,
                theme_position=tp,
                has_stop_loss=has_stop,
            )
            if (
                not v2_execution_allowed
                and trade_stage
                in (TradeStage.PROBE_ENTRY, TradeStage.ADD_ON_STRENGTH)
            ):
                trade_stage = TradeStage.FOCUS

            # Phase 3A: 交易计划
            trade_plan = plan_builder.build(
                trade_stage=trade_stage,
                setup_type=st,
                entry_maturity=entry_mat,
                risk_level=market_env.risk_level,
                pool_level=pool_level,
                factor_snapshot=fs,
            )

            # 写回 candidate
            candidate.trade_stage = trade_stage.value
            candidate.market_regime = market_env.regime.value
            candidate.entry_maturity = entry_mat.value
            candidate.setup_freshness = setup_freshness
            candidate.candidate_pool_level = pool_level.value
            candidate.theme_position = tp.value
            candidate.risk_level = market_env.risk_level.value
            candidate.theme_tag = theme_decision.theme_tag
            candidate.theme_score = theme_decision.theme_score
            candidate.leader_score = leader_score
            candidate.sector_strength = theme_decision.sector_strength
            candidate.theme_duration = theme_decision.theme_duration
            candidate.trade_theme_stage = getattr(theme_decision, "trade_theme_stage", "unknown")
            candidate.leader_stocks = list(theme_decision.leader_stocks)
            candidate.front_stocks = list(theme_decision.front_stocks)
            candidate.setup_hit_reasons = list(
                getattr(resolution, "contributing_strategies", [])
                if dispatcher is not None and setup_resolver is not None
                else []
            )
            fs["effective_leader_score"] = leader_score
            fs["effective_extreme_strength_score"] = extreme_strength
            fs["effective_leader_score_source"] = leader_score_source
            if trade_plan is not None:
                candidate.trade_plan_json = json.dumps(
                    trade_plan.to_payload(),
                    ensure_ascii=False,
                )

            # ── D1 核心: L5 否决权 ────────────────────────────────────
            if trade_stage in (TradeStage.REJECT, TradeStage.STAND_ASIDE):
                vetoed.append(candidate)
            else:
                kept.append(candidate)

        stats["vetoed_count"] = len(vetoed)
        stats["kept_count"] = len(kept)
        stage_counts = _counter_to_dict([str(c.trade_stage or "") for c in [*kept, *vetoed]])
        theme_position_counts = _counter_to_dict([str(c.theme_position or "") for c in [*kept, *vetoed]])
        setup_type_counts = _counter_to_dict(
            [str(c.setup_type or "none") for c in [*kept, *vetoed]]
        )
        pool_level_counts = _counter_to_dict(
            [str(c.candidate_pool_level or "") for c in [*kept, *vetoed]]
        )
        vetoed_stage_counts = _counter_to_dict([str(c.trade_stage or "") for c in vetoed])
        stats["trade_stage_counts"] = stage_counts
        stats["theme_position_counts"] = theme_position_counts
        stats["setup_type_counts"] = setup_type_counts
        stats["candidate_pool_level_counts"] = pool_level_counts
        stats["vetoed_stage_counts"] = vetoed_stage_counts
        logger.info(
            "pipeline L3-L5: kept=%d vetoed=%d trade_stages=%s themes=%s setups=%s pools=%s vetoed_stages=%s",
            len(kept),
            len(vetoed),
            stage_counts,
            theme_position_counts,
            setup_type_counts,
            pool_level_counts,
            vetoed_stage_counts,
        )
        # ── 五层优先级排序 (D6) ───────────────────────────────────────
        for c in kept:
            stage_p = _STAGE_PRIORITY.get(c.trade_stage or "", 0)
            pool_p = _POOL_PRIORITY.get(c.candidate_pool_level or "", 0)
            theme_p = _THEME_PRIORITY.get(c.theme_position or "", 0)
            c.rule_score = stage_p + pool_p + theme_p + c.rule_score * 0.01

        kept.sort(key=lambda c: c.rule_score, reverse=True)
        for i, c in enumerate(kept, 1):
            c.rank = i

        if kept:
            logger.info(
                "pipeline rerank top3: %s",
                [(c.code, round(c.rule_score, 1), c.trade_stage, c.theme_position) for c in kept[:3]],
            )

        # ── 构建 decision_context ─────────────────────────────────────
        decision_context = self._build_decision_context(
            market_env, guard_result, sector_results, theme_registry,
        )
        decision_context["pipeline_stats"] = stats

        return PipelineResult(
            candidates=kept,
            decision_context=decision_context,
            market_env=market_env,
            pipeline_stats=stats,
        )

    # ── 内部方法 ─────────────────────────────────────────────────────

    @staticmethod
    def _get_theme_member_codes(
        db_manager: Any, board_names: Set[str],
    ) -> Set[str]:
        """从 DB 获取板块成员代码集合。"""
        member_map = db_manager.batch_get_board_member_codes(
            list(board_names), market="cn",
        )
        codes: Set[str] = set()
        for members in member_map.values():
            codes.update(members)
        return codes

    @staticmethod
    def _build_decision_context(
        market_env: MarketEnvironment,
        guard_result: Any,
        sector_results: list,
        theme_registry: Any,
    ) -> Dict[str, Any]:
        return {
            "market_environment": {
                "market_regime": market_env.regime.value,
                "risk_level": market_env.risk_level.value,
                "index_price": getattr(market_env, "index_price", None),
                "index_ma100": getattr(market_env, "index_ma100", None),
                "is_safe": guard_result.is_safe if guard_result else None,
                "message": guard_result.message if guard_result else None,
            },
            "sector_heat_results": [
                {
                    "board_name": s.board_name,
                    "board_type": s.board_type,
                    "sector_hot_score": s.sector_hot_score,
                    "sector_status": s.sector_status,
                    "sector_stage": s.sector_stage,
                    "canonical_theme": (
                        theme_registry.resolve_tag(s.board_name) if theme_registry else s.board_name
                    ),
                    "stock_count": s.stock_count,
                    "up_count": s.up_count,
                    "limit_up_count": getattr(s, "limit_up_count", 0),
                    "board_strength_score": getattr(s, "board_strength_score", s.sector_hot_score),
                    "board_strength_rank": getattr(s, "board_strength_rank", 0),
                    "board_strength_percentile": getattr(s, "board_strength_percentile", 0.0),
                    "leader_candidate_count": getattr(s, "leader_candidate_count", 0),
                    "quality_flags": dict(getattr(s, "quality_flags", {}) or {}),
                }
                for s in sector_results
            ],
            "hot_theme_count": sum(1 for s in sector_results if s.sector_status == "hot"),
            "warm_theme_count": sum(1 for s in sector_results if s.sector_status == "warm"),
        }
