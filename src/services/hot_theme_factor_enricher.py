# -*- coding: utf-8 -*-
"""Hot theme factor enrichment for FactorService."""

from typing import Dict, Any, List, Optional

from src.services.theme_matching_service import ThemeMatchingService
from src.services.leader_score_calculator import LeaderScoreCalculator
from src.services.extreme_strength_scorer import (
    ExtremeStrengthScorer,
    LayeredExtremeStrengthScores,
)
from src.services.extreme_strength_timing_assessor import (
    ExtremeStrengthTimingAssessor,
    StageLabel,
    TimingAssessment,
)
from src.services.theme_context_ingest_service import OpenClawThemeContext
from src.services.core_signal_identifier import CoreSignalIdentifier, SignalKind


# 分层评分 snapshot 字段的默认值，用于 no-theme / cold-theme 分支。
# 与 ExtremeStrengthScorer.calculate_layered_scores 的四桶字段保持对齐。
_LAYERED_DEFAULTS: Dict[str, Any] = {
    "theme_pool_score": 0.0,
    "leadership_score": 0.0,
    "entry_signal_score": 0.0,
    "timing_penalty": 0.0,
    # A7：把 leader_score 与其它桶之间的重复加权估算值和去重后的净总分
    # 也作为 snapshot 字段暴露，供 UI/LLM 选择性展示。`extreme_strength_score`
    # 本身保持与旧版一致，以保护所有 >=50/>=60/>=80 阈值契约。
    "leader_double_count": 0.0,
    "extreme_strength_score_deduplicated": 0.0,
    "extreme_strength_breakdown": {},
}

# A3 新增：时机评估结果的默认值（pool_only 阶段 + 无惩罚 + 空原因列表）。
_TIMING_DEFAULTS: Dict[str, Any] = {
    "stage_label": StageLabel.POOL_ONLY.value,
    "timing_reasons": [],
    "timing_bars_since_event": -1,
    "timing_extended_pct": 0.0,
}

# A4 新增：信号分类结果的默认值（无信号 + 空列表），保证 snapshot schema 一致。
_SIGNAL_KIND_DEFAULTS: Dict[str, Any] = {
    "primary_signal": None,
    "signal_kind": SignalKind.NONE.value,
    "all_signals": [],
}

# A5 新增：StageLabel → 中文标签映射。entry_reason 将以该中文标签为前缀，
# 让前端/通知直接表达 "仅观察 / 突破当日 / 回踩确认 / 已走远·勿追" 等语义。
# 顺序保持与 StageLabel 枚举声明一致，便于后续 UI 侧反查。
_STAGE_LABEL_CN: Dict[str, str] = {
    StageLabel.POOL_ONLY.value: "池子层",
    StageLabel.WATCH_ONLY.value: "仅观察",
    StageLabel.BREAKOUT_DAY.value: "突破当日",
    StageLabel.RETEST_ENTRY.value: "回踩确认",
    StageLabel.EXTENDED_DO_NOT_CHASE.value: "已走远·勿追",
}


class HotThemeFactorEnricher:
    """Enrich factor snapshots with hot theme context."""

    PHASE_LABELS = {
        "phase1_market_and_theme": "阶段1: 市场与题材",
        "phase2_leader_screen": "阶段2: 龙头筛选",
        "phase3_core_signal": "阶段3: 核心信号",
        "phase4_entry_readiness": "阶段4: 入场准备",
        "phase5_risk_controls": "阶段5: 风险控制",
    }

    def __init__(self) -> None:
        """Initialize enricher with scoring services."""
        self.theme_matcher = ThemeMatchingService()
        self.leader_calculator = LeaderScoreCalculator()
        self.strength_scorer = ExtremeStrengthScorer()
        self.signal_identifier = CoreSignalIdentifier()
        self.timing_assessor = ExtremeStrengthTimingAssessor()

    def enrich_snapshot(
        self,
        snapshot: Dict[str, Any],
        theme_context: Optional[OpenClawThemeContext],
        boards: Optional[List[str]] = None,
        normalized_themes: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Enrich factor snapshot with hot theme fields.
        Returns enriched snapshot with theme-related fields.
        """
        base_leader_score = float(snapshot.get("base_leader_score", snapshot.get("leader_score", 0.0)) or 0.0)
        base_extreme_strength_score = float(
            snapshot.get(
                "base_extreme_strength_score",
                snapshot.get("extreme_strength_score", 0.0),
            )
            or 0.0
        )
        snapshot["base_leader_score"] = base_leader_score
        snapshot["base_extreme_strength_score"] = base_extreme_strength_score

        if theme_context is None or not theme_context.themes:
            # No theme context, add default values
            snapshot["is_hot_theme_stock"] = False
            snapshot["primary_theme"] = None
            snapshot["theme_tags"] = []
            snapshot["theme_heat_score"] = 0.0
            snapshot["theme_match_score"] = 0.0
            snapshot["theme_leader_score"] = 0.0
            snapshot["theme_extreme_strength_score"] = 0.0
            snapshot["leader_score"] = base_leader_score
            snapshot["extreme_strength_score"] = base_extreme_strength_score
            snapshot["leader_score_source"] = "base"
            snapshot["extreme_strength_score_source"] = "base"
            snapshot["extreme_strength_reasons"] = []
            snapshot["entry_reason"] = None
            snapshot["core_signal"] = None
            snapshot["bonus_signals"] = []
            snapshot["theme_catalyst_summary"] = None
            snapshot["theme_catalyst_news"] = []
            self._apply_layered_defaults(snapshot)
            self._apply_timing_defaults(snapshot)
            self._apply_signal_kind_defaults(snapshot)
            snapshot["phase_results"] = self._build_phase_results(
                market_and_theme=False,
                leader_screen=False,
                core_signal=False,
                entry_readiness=False,
                risk_controls=False,
            )
            snapshot["risk_params"] = {"stop_loss": 0, "position_size": "无", "take_profit_ratio": 0}
            snapshot["phase_explanations"] = self._build_phase_explanations(
                phase_results=snapshot["phase_results"],
                primary_theme=None,
                theme_match_score=0.0,
                theme_heat_score=0.0,
                leader_score=base_leader_score,
                core_signal=None,
                entry_reason=None,
                risk_params=snapshot["risk_params"],
                extreme_strength_score=base_extreme_strength_score,
            )
            return snapshot

        stock_name = snapshot.get("name", "")
        boards = boards or []

        # Build normalized board lookup for each theme
        norm_board_map: Dict[str, List[str]] = {}
        if normalized_themes:
            for nt in normalized_themes:
                raw = nt.get("raw_theme", "")
                if raw and nt.get("matched_boards"):
                    norm_board_map[raw] = nt["matched_boards"]

        # Find best matching theme
        best_theme = None
        best_match_score = 0.0
        best_theme_heat = 0.0

        for theme in theme_context.themes:
            normalized_boards = norm_board_map.get(theme.name)

            if normalized_boards:
                # Use normalized boards: match stock boards against each
                # normalized board as the theme target.
                theme_score = 0.0
                for norm_board in normalized_boards:
                    score = self.theme_matcher.calculate_theme_match_score(
                        boards=boards,
                        stock_name=stock_name,
                        theme_name=norm_board,
                        keywords=theme.keywords,
                    )
                    theme_score = max(theme_score, score)
            else:
                # Fallback: use raw theme name (original behavior)
                theme_score = self.theme_matcher.calculate_theme_match_score(
                    boards=boards,
                    stock_name=stock_name,
                    theme_name=theme.name,
                    keywords=theme.keywords,
                )

            if theme_score > best_match_score:
                best_match_score = theme_score
                best_theme = theme
                best_theme_heat = theme.heat_score

        # Check if hot theme stock
        is_hot = best_match_score >= self.theme_matcher.THEME_MATCH_THRESHOLD

        # Calculate leader score
        theme_leader_score = 0.0
        if is_hot:
            circ_mv = snapshot.get("circ_mv")
            turnover_rate = snapshot.get("turnover_rate")
            theme_leader_score = self.leader_calculator.calculate_leader_score(
                theme_match_score=best_match_score,
                circ_mv=circ_mv,
                turnover_rate=turnover_rate,
                is_limit_up=snapshot.get("is_limit_up", False),
                gap_breakaway=snapshot.get("gap_breakaway", False),
                above_ma100=snapshot.get("above_ma100", False),
                ma100_breakout_days=snapshot.get("ma100_breakout_days", 0),
            )

        # A3: 时机评估，无论是否 hot 都先计算，便于 non-hot 分支也能落 stage_label
        timing_assessment: TimingAssessment = self.timing_assessor.assess_from_snapshot(
            snapshot,
            is_hot_theme=is_hot,
        )

        # Calculate extreme strength score using corrected strategy
        theme_extreme_strength_score = 0.0
        layered_scores: Optional[LayeredExtremeStrengthScores] = None
        extreme_strength_reasons = []
        entry_reason = None
        core_signal = None
        bonus_signals = []
        primary_signal: Optional[str] = None
        primary_signal_kind: str = SignalKind.NONE.value
        all_signals: List[Dict[str, Any]] = []

        if is_hot:
            # A4：统一信号分类入口，按 signal_kind 跨类别选 primary_signal。
            signal_classification = self.signal_identifier.classify_signals(
                has_gap=snapshot.get("gap_breakaway", False),
                has_limit_up=snapshot.get("is_limit_up", False),
                has_gap_breakout_ma100=snapshot.get("gap_breakaway", False)
                and snapshot.get("above_ma100", False),
                has_low_123_breakout=snapshot.get("pattern_123_low_trendline", False),
                has_bottom_divergence=snapshot.get("bottom_divergence_double_breakout", False),
            )
            # 旧字段 (core_result / bonus_result) 从统一分类中拆回，保证
            # extreme_strength_reasons 合并顺序与旧实现一致。
            core_result = {
                "core_signal": signal_classification["core_signal"],
                "core_signal_score": signal_classification["core_signal_score"],
                "signal_kind": signal_classification["core_signal_kind"],
                "hit_reasons": [
                    r
                    for r in signal_classification["hit_reasons"]
                    if r in {"跳空涨停（缺口+涨停共振）", "缺口突破MA100均线", "涨停"}
                ],
            }
            bonus_result = {
                "bonus_signals": signal_classification["bonus_signals"],
                "bonus_score": signal_classification["bonus_score"],
                "signal_kinds": signal_classification["bonus_signal_kinds"],
                "hit_reasons": [
                    r
                    for r in signal_classification["hit_reasons"]
                    if r in {"低位123结构+涨停突破高点2", "底背离双突破"}
                ],
            }
            primary_signal = signal_classification["primary_signal"]
            primary_signal_kind = signal_classification["primary_signal_kind"]
            all_signals = signal_classification["all_signals"]

            # Calculate layered scores (A1/A2 契约：total_score 与旧总分一致)
            # A3：接入 timing_penalty，使"强但走远"的股票自然降级到合理排位。
            layered_scores = self.strength_scorer.calculate_layered_scores(
                above_ma100=snapshot.get("above_ma100", False),
                gap_breakaway=snapshot.get("gap_breakaway", False),
                pattern_123_low_trendline=snapshot.get("pattern_123_low_trendline", False),
                pattern_123_watchlist=snapshot.get("pattern_123_watchlist", False),
                is_limit_up=snapshot.get("is_limit_up", False),
                bottom_divergence_double_breakout=snapshot.get("bottom_divergence_double_breakout", False),
                theme_heat_score=best_theme_heat,
                leader_score=theme_leader_score,
                volume_ratio=snapshot.get("volume_ratio", 0.0) or 0.0,
                turnover_rate=snapshot.get("turnover_rate"),
                circ_mv=snapshot.get("circ_mv"),
                breakout_ratio=snapshot.get("breakout_ratio", 0.0) or 0.0,
                timing_penalty=timing_assessment.timing_penalty,
            )
            theme_extreme_strength_score = layered_scores.total_score

            core_signal = core_result["core_signal"]
            bonus_signals = bonus_result["bonus_signals"]
            extreme_strength_reasons = core_result["hit_reasons"] + bonus_result["hit_reasons"]
            if snapshot.get("above_ma100"):
                extreme_strength_reasons.append("MA100之上")
            if snapshot.get("gap_breakaway"):
                extreme_strength_reasons.append("跳空突破")
            if snapshot.get("is_limit_up"):
                extreme_strength_reasons.append("涨停")
            extreme_strength_reasons = list(dict.fromkeys(extreme_strength_reasons))

            # A5：entry_reason 以 stage_label 中文为主导，旧描述词（开盘半小时内
            # 涨停 / 刚突破 MA100）仅作为上下文后缀拼接，避免下游把
            # "强但走远" 的股票误读为 "当前可买"。
            entry_reason = self._compose_entry_reason(snapshot, timing_assessment)

        # Build phase results。A5：entry_readiness 使用 stage_label 作为硬闸，
        # 确保 extended_do_not_chase 不会被勾选为 "可入场"，即使总分很高。
        phase_results = self._build_phase_results(
            market_and_theme=is_hot,
            leader_screen=is_hot and theme_leader_score >= 50,
            core_signal=is_hot and core_signal is not None,
            entry_readiness=(
                is_hot
                and theme_extreme_strength_score >= 60
                and timing_assessment.stage_label != StageLabel.EXTENDED_DO_NOT_CHASE
            ),
            risk_controls=is_hot,
        )

        # A6：risk_params 按 primary_signal 引用真实子信号的止损依据，
        # 并根据 stage_label 控制仓位；不再统一套用 MA100*0.95 的硬模板。
        risk_params = self._resolve_risk_params(
            snapshot,
            primary_signal=primary_signal if is_hot else None,
            timing_assessment=timing_assessment,
            extreme_strength_score=theme_extreme_strength_score,
            is_hot=is_hot,
        )

        # Extract news from theme evidence
        theme_catalyst_news = []
        if best_theme and best_theme.evidence:
            theme_catalyst_news = [
                {
                    "title": e.get("title", ""),
                    "source": e.get("source", ""),
                    "url": e.get("url"),
                    "published_at": e.get("published_at"),
                    "heat_score": best_theme_heat,
                }
                for e in best_theme.evidence
            ]

        # Enrich snapshot
        snapshot["is_hot_theme_stock"] = is_hot
        snapshot["primary_theme"] = best_theme.name if best_theme else None
        snapshot["theme_tags"] = [best_theme.name] if best_theme else []
        snapshot["theme_heat_score"] = best_theme_heat
        snapshot["theme_match_score"] = best_match_score
        effective_leader_score, effective_extreme_strength_score = self._resolve_effective_scores(
            base_leader_score=base_leader_score,
            base_extreme_strength_score=base_extreme_strength_score,
            theme_leader_score=theme_leader_score if is_hot else 0.0,
            theme_extreme_strength_score=theme_extreme_strength_score if is_hot else 0.0,
        )
        snapshot["theme_leader_score"] = theme_leader_score
        snapshot["theme_extreme_strength_score"] = theme_extreme_strength_score
        snapshot["leader_score"] = effective_leader_score
        snapshot["extreme_strength_score"] = effective_extreme_strength_score
        snapshot["leader_score_source"] = "theme" if theme_leader_score > 0.0 and is_hot else "base"
        snapshot["extreme_strength_score_source"] = (
            "theme" if theme_extreme_strength_score > 0.0 and is_hot else "base"
        )
        snapshot["extreme_strength_reasons"] = extreme_strength_reasons
        snapshot["theme_catalyst_summary"] = best_theme.catalyst_summary if best_theme else None
        snapshot["theme_catalyst_news"] = theme_catalyst_news
        snapshot["entry_reason"] = entry_reason
        snapshot["core_signal"] = core_signal
        snapshot["bonus_signals"] = bonus_signals
        self._apply_layered_fields(snapshot, layered_scores if is_hot else None)
        self._apply_timing_fields(snapshot, timing_assessment)
        self._apply_signal_kind_fields(
            snapshot,
            primary_signal=primary_signal if is_hot else None,
            primary_signal_kind=primary_signal_kind if is_hot else SignalKind.NONE.value,
            all_signals=all_signals if is_hot else [],
        )
        snapshot["phase_results"] = phase_results
        snapshot["phase_explanations"] = self._build_phase_explanations(
            phase_results=phase_results,
            primary_theme=best_theme.name if best_theme else None,
            theme_match_score=best_match_score,
            theme_heat_score=best_theme_heat,
            leader_score=effective_leader_score,
            core_signal=core_signal,
            entry_reason=entry_reason,
            risk_params=risk_params,
            extreme_strength_score=effective_extreme_strength_score,
        )
        snapshot["risk_params"] = risk_params

        return snapshot

    @staticmethod
    def _apply_layered_defaults(snapshot: Dict[str, Any]) -> None:
        """为 no-theme / cold-theme 分支写入分层评分默认值，保证 snapshot 模式一致。"""
        for key, default in _LAYERED_DEFAULTS.items():
            snapshot[key] = {} if isinstance(default, dict) else default

    @staticmethod
    def _apply_layered_fields(
        snapshot: Dict[str, Any],
        layered: Optional[LayeredExtremeStrengthScores],
    ) -> None:
        """把 LayeredExtremeStrengthScores 写入 snapshot，未命中 hot 时回落到默认值。"""
        if layered is None:
            for key, default in _LAYERED_DEFAULTS.items():
                snapshot[key] = {} if isinstance(default, dict) else default
            return
        snapshot["theme_pool_score"] = layered.theme_pool_score
        snapshot["leadership_score"] = layered.leadership_score
        snapshot["entry_signal_score"] = layered.entry_signal_score
        snapshot["timing_penalty"] = layered.timing_penalty
        # A7：重复加权估算值 + 去重后的净总分。命名上沿用 snapshot 已有的
        # `extreme_strength_score_*` 前缀，方便前端一眼识别。
        snapshot["leader_double_count"] = layered.leader_double_count
        snapshot["extreme_strength_score_deduplicated"] = (
            layered.deduplicated_total_score
        )
        snapshot["extreme_strength_breakdown"] = dict(layered.breakdown)

    @staticmethod
    def _apply_timing_defaults(snapshot: Dict[str, Any]) -> None:
        """为 no-theme 分支写入时机评估默认值（pool_only + 0 penalty）。"""
        for key, default in _TIMING_DEFAULTS.items():
            snapshot[key] = list(default) if isinstance(default, list) else default

    @staticmethod
    def _apply_signal_kind_defaults(snapshot: Dict[str, Any]) -> None:
        """为 no-theme / non-hot 分支写入 signal_kind 默认值。"""
        for key, default in _SIGNAL_KIND_DEFAULTS.items():
            snapshot[key] = list(default) if isinstance(default, list) else default

    @staticmethod
    def _apply_signal_kind_fields(
        snapshot: Dict[str, Any],
        *,
        primary_signal: Optional[str],
        primary_signal_kind: str,
        all_signals: List[Dict[str, Any]],
    ) -> None:
        """把统一分类结果写入 snapshot（primary_signal / signal_kind / all_signals）。"""
        snapshot["primary_signal"] = primary_signal
        snapshot["signal_kind"] = primary_signal_kind
        snapshot["all_signals"] = [dict(s) for s in all_signals]

    @staticmethod
    def _apply_timing_fields(
        snapshot: Dict[str, Any],
        timing: TimingAssessment,
    ) -> None:
        """把 TimingAssessment 写入 snapshot，供下游 UI / 解释消费。"""
        snapshot["stage_label"] = timing.stage_label.value
        snapshot["timing_reasons"] = list(timing.reasons)
        snapshot["timing_bars_since_event"] = timing.bars_since_primary_event
        snapshot["timing_extended_pct"] = timing.extended_pct

    @staticmethod
    def _resolve_effective_scores(
        base_leader_score: float,
        base_extreme_strength_score: float,
        theme_leader_score: float,
        theme_extreme_strength_score: float,
    ) -> tuple[float, float]:
        effective_leader_score = (
            theme_leader_score if theme_leader_score > 0.0 else base_leader_score
        )
        effective_extreme_strength_score = (
            theme_extreme_strength_score
            if theme_extreme_strength_score > 0.0
            else base_extreme_strength_score
        )
        return effective_leader_score, effective_extreme_strength_score

    @staticmethod
    def _compose_entry_reason(
        snapshot: Dict[str, Any],
        timing: TimingAssessment,
    ) -> Optional[str]:
        """把 stage_label 中文标签和旧描述词合成单一 entry_reason 字符串。

        语义：
        - 阶段标签始终作为前缀，保证下游一眼能看出 "观察 / 突破当日 / 回踩 / 勿追"。
        - 旧描述词（开盘半小时内涨停 / 刚突破 MA100）退化为可选上下文后缀。
        - ``pool_only`` 只出现在非热点分支，这里只会在 ``is_hot`` 调用方传入，所以
          不会返回 ``池子层``，无需额外分支。
        """
        stage_cn = _STAGE_LABEL_CN.get(timing.stage_label.value)
        suffix: Optional[str] = None
        intraday_minutes = snapshot.get("intraday_minutes_since_open")
        if (
            snapshot.get("is_limit_up")
            and isinstance(intraday_minutes, (int, float))
            and intraday_minutes <= 30
        ):
            suffix = "开盘半小时内涨停"
        elif snapshot.get("above_ma100"):
            suffix = "刚突破MA100"

        if stage_cn and suffix:
            return f"{stage_cn} · {suffix}"
        if stage_cn:
            return stage_cn
        return suffix

    @staticmethod
    def _resolve_stop_loss(
        snapshot: Dict[str, Any],
        primary_signal: Optional[str],
    ) -> tuple[float, str]:
        """按 primary_signal 引用对应子信号的真实止损依据。

        Returns:
            (stop_loss_price, basis_label)。price<=0 表示未命中任何专属依据，
            由调用方回退到 MA100×0.95 模板。
        """

        def _to_float(value: Any) -> Optional[float]:
            if value is None:
                return None
            try:
                price = float(value)
            except (TypeError, ValueError):
                return None
            return price if price > 0 else None

        if primary_signal == "低位123结构":
            price = _to_float(snapshot.get("pattern_123_stop_loss"))
            if price is None:
                price = _to_float(snapshot.get("pattern_123_pullback_support_price"))
            if price is not None:
                return price, "123结构低点"
        elif primary_signal == "底背离双突破":
            price = _to_float(snapshot.get("bottom_divergence_stop_loss"))
            if price is not None:
                return price, "底背离临界区"
        elif primary_signal == "缺口突破MA100":
            price = _to_float(snapshot.get("breakaway_gap_low"))
            if price is not None:
                return price, "缺口下沿"
        elif primary_signal in {"跳空涨停", "涨停"}:
            price = _to_float(snapshot.get("limitup_key_level_price"))
            if price is not None:
                level_type = snapshot.get("limitup_key_level_type") or "关键位"
                return price, f"涨停前{level_type}"
            gap_low = _to_float(snapshot.get("breakaway_gap_low"))
            if gap_low is not None:
                return gap_low, "缺口下沿"
        return 0.0, "none"

    def _resolve_risk_params(
        self,
        snapshot: Dict[str, Any],
        *,
        primary_signal: Optional[str],
        timing_assessment: TimingAssessment,
        extreme_strength_score: float,
        is_hot: bool,
    ) -> Dict[str, Any]:
        """按子信号 / 阶段标签分层给出风险参数，新增 ``stop_loss_basis`` 字段。

        - 子信号有真实止损 → 用专属止损价，basis 反映依据来源
        - 命中热点但无专属止损 → 回退到 MA100 × 0.95 + basis="MA100×0.95"
        - 非热点 → stop_loss=0，与旧 "轻仓试错 / 可加仓" 的模板保持向后兼容
        - 阶段为 extended_do_not_chase → 强制 position_size=不建议入场、take_profit=0
        """
        stop_loss, basis = self._resolve_stop_loss(snapshot, primary_signal)

        if stop_loss <= 0:
            if is_hot and snapshot.get("above_ma100"):
                ma100 = float(snapshot.get("ma100", 0) or 0)
                if ma100 > 0:
                    stop_loss = round(ma100 * 0.95, 4)
                    basis = "MA100×0.95"
                else:
                    basis = "none"
            else:
                basis = "none"

        if timing_assessment.stage_label == StageLabel.EXTENDED_DO_NOT_CHASE:
            position_size = "不建议入场"
            take_profit_ratio = 0.0
        elif not is_hot:
            position_size = "无"
            take_profit_ratio = 0.0
        elif extreme_strength_score < 80:
            position_size = "轻仓试错"
            take_profit_ratio = 0.10
        else:
            position_size = "可加仓"
            take_profit_ratio = 0.15

        return {
            "stop_loss": stop_loss,
            "stop_loss_basis": basis,
            "position_size": position_size,
            "take_profit_ratio": take_profit_ratio,
        }

    @staticmethod
    def _build_phase_results(
        market_and_theme: bool,
        leader_screen: bool,
        core_signal: bool,
        entry_readiness: bool,
        risk_controls: bool,
    ) -> Dict[str, bool]:
        return {
            "phase1_market_and_theme": market_and_theme,
            "phase2_leader_screen": leader_screen,
            "phase3_core_signal": core_signal,
            "phase4_entry_readiness": entry_readiness,
            "phase5_risk_controls": risk_controls,
        }

    def _build_phase_explanations(
        self,
        phase_results: Dict[str, bool],
        primary_theme: Optional[str],
        theme_match_score: float,
        theme_heat_score: float,
        leader_score: float,
        core_signal: Optional[str],
        entry_reason: Optional[str],
        risk_params: Dict[str, Any],
        extreme_strength_score: float,
    ) -> List[Dict[str, Any]]:
        stop_loss = float(risk_params.get("stop_loss", 0) or 0)
        position_size = risk_params.get("position_size", "-")
        take_profit_ratio = float(risk_params.get("take_profit_ratio", 0) or 0)

        summaries = {
            "phase1_market_and_theme": (
                f"theme={primary_theme or '-'}; "
                f"theme_match_score={theme_match_score:.2f}; "
                f"theme_heat_score={theme_heat_score:.1f}"
                if phase_results["phase1_market_and_theme"]
                else "未通过热点题材匹配门槛"
            ),
            "phase2_leader_screen": (
                f"leader_score={leader_score}"
                if phase_results["phase2_leader_screen"]
                else f"leader_score={leader_score}; 仍未达到龙头筛选阈值"
            ),
            "phase3_core_signal": (
                f"core_signal={core_signal or '-'}"
                if phase_results["phase3_core_signal"]
                else "缺少关键缺口/涨停共振信号"
            ),
            "phase4_entry_readiness": (
                f"entry_reason={entry_reason or '-'}; extreme_strength_score={extreme_strength_score:.1f}"
                if phase_results["phase4_entry_readiness"]
                else f"等待入场确认; extreme_strength_score={extreme_strength_score:.1f}"
            ),
            "phase5_risk_controls": (
                f"stop_loss={stop_loss:.2f}; position_size={position_size}; "
                f"take_profit_ratio={take_profit_ratio:.2f}"
                if phase_results["phase5_risk_controls"]
                else "尚未形成可执行的风险控制参数"
            ),
        }

        return [
            {
                "phase_key": phase_key,
                "label": label,
                "hit": phase_results.get(phase_key, False),
                "summary": summaries[phase_key],
            }
            for phase_key, label in self.PHASE_LABELS.items()
        ]
