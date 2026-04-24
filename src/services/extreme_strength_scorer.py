# -*- coding: utf-8 -*-
"""Extreme strength scorer for hot theme stocks."""

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class LayeredExtremeStrengthScores:
    """分层极端强势评分结果。

    按 extreme_strength 复核计划 §2 的四桶划分，用于让上层/UI 能清楚看到
    总分是如何构成的，避免把"总分高"误解为"当前可买"。

    - theme_pool_score: 热点池维度贡献（题材热度）
    - leadership_score: 龙头属性贡献（leader_score 及市场活跃度因子）
    - entry_signal_score: 趋势背景 + 结构/事件买点贡献
    - timing_penalty: 时机惩罚（<= 0，阶段 A3 接入真实计算，当前为占位）
    - total_score: 以上四项之和，等价于旧 calculate_extreme_strength_score 返回值
    - leader_double_count: 阶段 A7 新增。`leader_score` 与其它桶之间可观测的
      重复加权估算值（已按 leader_contribution 的 0.15 缩放）。仅对
      `small_circ_mv / turnover / breakout_strength(is_limit_up+gap_breakaway) /
      trend_strength(above_ma100 保守下界)` 四项做精确复刻，`theme_match`
      因无法从 scalar leader_score 回推而不计入。
    - deduplicated_total_score: 阶段 A7 新增。`total_score - leader_double_count`，
      用于可选的去重视图；`total_score` 本身不变，所有既有阈值契约保持。
    - breakdown: 各细分子项的原始贡献分，便于 UI/审计逐项溯源
    """

    theme_pool_score: float
    leadership_score: float
    entry_signal_score: float
    timing_penalty: float
    total_score: float
    leader_double_count: float = 0.0
    deduplicated_total_score: float = 0.0
    breakdown: Dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ExtremeStrengthScorer:
    """Calculate extreme strength score for hot theme stocks."""

    SELECTED_THRESHOLD = 80
    WATCHLIST_THRESHOLD_MIN = 60
    WATCHLIST_THRESHOLD_MAX = 80

    def calculate_base_score(self, above_ma100: bool) -> float:
        """Calculate base score (0-20 points)."""
        return 20.0 if above_ma100 else 0.0

    def calculate_signal_bonus(
        self,
        gap_breakaway: bool,
        pattern_123_low_trendline: bool,
        is_limit_up: bool,
        bottom_divergence_double_breakout: bool,
        pattern_123_watchlist: bool = False,
    ) -> float:
        """
        Calculate signal bonus (0-49 points).
        - gap_breakaway: 15
        - pattern_123_low_trendline: 12
        - pattern_123_watchlist: 8
        - is_limit_up: 10
        - bottom_divergence_double_breakout: 12
        """
        score = 0.0
        if gap_breakaway:
            score += 15.0
        if pattern_123_low_trendline:
            score += 12.0
        elif pattern_123_watchlist:
            score += 8.0
        if is_limit_up:
            score += 10.0
        if bottom_divergence_double_breakout:
            score += 12.0
        return score

    def calculate_auxiliary_bonus(
        self,
        theme_heat_score: float,
        leader_score: float,
        volume_ratio: float,
        turnover_rate: Optional[float],
        circ_mv: Optional[float],
        breakout_ratio: float,
    ) -> float:
        """
        Calculate auxiliary bonus (0-53 points).
        - theme_heat_score: 0-10 (0-100 normalized)
        - leader_score: 0-15 (0-100 normalized)
        - volume_ratio: 0-8 (0.5-2.0 normalized)
        - turnover_rate: 0-6 (0-10% normalized)
        - circ_mv: 0-6 (small market cap bonus)
        - breakout_ratio: 0-8 (0.5-2.0 normalized)
        """
        score = 0.0

        # Theme heat score (0-100 -> 0-10)
        score += min(10.0, theme_heat_score * 0.1)

        # Leader score (0-100 -> 0-15)
        score += min(15.0, leader_score * 0.15)

        # Volume ratio (0.5-2.0 -> 0-8)
        if volume_ratio >= 1.0:
            score += min(8.0, (volume_ratio - 1.0) * 8.0)

        # Turnover rate (0-10% -> 0-6)
        normalized_turnover = self._normalize_turnover_rate(turnover_rate)
        if normalized_turnover is not None:
            score += min(6.0, normalized_turnover * 60.0)

        # Small circulation market value (< 50B -> 6)
        if circ_mv is not None and circ_mv < 50_000_000_000:
            score += 6.0
        elif circ_mv is not None and circ_mv < 100_000_000_000:
            score += 3.0

        # Breakout ratio (0.5-2.0 -> 0-8)
        if breakout_ratio >= 1.0:
            score += min(8.0, (breakout_ratio - 1.0) * 8.0)

        return score

    def calculate_layered_scores(
        self,
        above_ma100: bool,
        gap_breakaway: bool,
        pattern_123_low_trendline: bool,
        is_limit_up: bool,
        bottom_divergence_double_breakout: bool,
        theme_heat_score: float,
        leader_score: float,
        volume_ratio: float,
        turnover_rate: Optional[float],
        circ_mv: Optional[float],
        breakout_ratio: float,
        pattern_123_watchlist: bool = False,
        timing_penalty: float = 0.0,
    ) -> LayeredExtremeStrengthScores:
        """计算分层评分结果（不破坏既有总分）。

        总分 = theme_pool + leadership + entry_signal + timing_penalty，
        与旧 `calculate_extreme_strength_score` 数值等价（当 timing_penalty=0 时）。

        阶段 A1 的职责是"把总分拆出来可见"，不调整任何权重；
        timing_penalty 作为接入占位，阶段 A3 再由时机评估器负向压分。
        """
        # ── 趋势背景 + 结构/事件买点（旧 base + signal_bonus） ────────────────
        base_score = self.calculate_base_score(above_ma100)
        signal_bonus = self.calculate_signal_bonus(
            gap_breakaway=gap_breakaway,
            pattern_123_low_trendline=pattern_123_low_trendline,
            is_limit_up=is_limit_up,
            bottom_divergence_double_breakout=bottom_divergence_double_breakout,
            pattern_123_watchlist=pattern_123_watchlist,
        )
        entry_signal_score = base_score + signal_bonus

        # ── 热点池贡献（题材热度） ──────────────────────────────────────────
        theme_pool_score = min(10.0, max(0.0, theme_heat_score) * 0.1)

        # ── 龙头 + 活跃度（原 auxiliary 减去 theme_heat 部分） ───────────────
        leader_contribution = min(15.0, max(0.0, leader_score) * 0.15)

        volume_contribution = 0.0
        if volume_ratio >= 1.0:
            volume_contribution = min(8.0, (volume_ratio - 1.0) * 8.0)

        turnover_contribution = 0.0
        normalized_turnover = self._normalize_turnover_rate(turnover_rate)
        if normalized_turnover is not None:
            turnover_contribution = min(6.0, normalized_turnover * 60.0)

        circ_mv_contribution = 0.0
        if circ_mv is not None and circ_mv < 50_000_000_000:
            circ_mv_contribution = 6.0
        elif circ_mv is not None and circ_mv < 100_000_000_000:
            circ_mv_contribution = 3.0

        breakout_contribution = 0.0
        if breakout_ratio >= 1.0:
            breakout_contribution = min(8.0, (breakout_ratio - 1.0) * 8.0)

        leadership_score = (
            leader_contribution
            + volume_contribution
            + turnover_contribution
            + circ_mv_contribution
            + breakout_contribution
        )

        # timing_penalty 必须是非正数（负反馈），这里做一次宽容校正
        penalty = min(0.0, float(timing_penalty))

        total_score = (
            theme_pool_score
            + leadership_score
            + entry_signal_score
            + penalty
        )

        # A7：估算 leader_contribution 中与其它桶重复加权的部分。
        # 保持 total_score 不变，仅把估算值 + 去重后的净总分作为只读字段暴露出来，
        # 供 UI / LLM 在需要时做 "净分" 对比。
        leader_double_count = self._estimate_leader_double_count(
            above_ma100=above_ma100,
            gap_breakaway=gap_breakaway,
            is_limit_up=is_limit_up,
            turnover_rate=turnover_rate,
            circ_mv=circ_mv,
        )
        # 保险：不能超过 leader_contribution 本身（否则会出现负的 leader 桶）
        leader_double_count = min(leader_double_count, leader_contribution)
        deduplicated_total_score = total_score - leader_double_count

        breakdown = {
            "base_score": base_score,
            "signal_bonus": signal_bonus,
            "theme_heat_contribution": theme_pool_score,
            "leader_contribution": leader_contribution,
            "volume_contribution": volume_contribution,
            "turnover_contribution": turnover_contribution,
            "circ_mv_contribution": circ_mv_contribution,
            "breakout_contribution": breakout_contribution,
            "leader_double_count": leader_double_count,
        }

        return LayeredExtremeStrengthScores(
            theme_pool_score=theme_pool_score,
            leadership_score=leadership_score,
            entry_signal_score=entry_signal_score,
            timing_penalty=penalty,
            total_score=total_score,
            leader_double_count=leader_double_count,
            deduplicated_total_score=deduplicated_total_score,
            breakdown=breakdown,
        )

    @staticmethod
    def _estimate_leader_double_count(
        *,
        above_ma100: bool,
        gap_breakaway: bool,
        is_limit_up: bool,
        turnover_rate: Optional[float],
        circ_mv: Optional[float],
    ) -> float:
        """按 LeaderScoreCalculator 的权重表回推 leader_score 中与其它桶重叠的部分。

        规则（原始 0-100 尺度，最后按 0.15 缩放回 leader_contribution 的量纲）：
        - small_circ_mv：<50B=20, 50-100B=10, 否则 0 —— 与 circ_mv_contribution 重复
        - turnover：>5%=20, 2-5%=10, 否则 0 —— 与 turnover_contribution 重复
        - breakout_strength：两项命中=15, 一项=10, 否则 0 —— 与 signal_bonus(is_limit_up
          + gap_breakaway) 重复
        - trend_strength：above_ma100 至少保守按 5 分计（不含 ma100_breakout_days
          <= 5 的 10 分满分情况，避免对未知 bars 过度推断）—— 与 base_score 重复
        - theme_match：leader_score 只以 scalar 传入，无法精确回推，不计入。
        """
        dup = 0.0

        if circ_mv is not None:
            if circ_mv < 50_000_000_000:
                dup += 20.0
            elif circ_mv < 100_000_000_000:
                dup += 10.0

        normalized_turnover: Optional[float]
        if turnover_rate is None:
            normalized_turnover = None
        elif turnover_rate > 1:
            normalized_turnover = turnover_rate / 100.0
        else:
            normalized_turnover = turnover_rate
        if normalized_turnover is not None:
            if normalized_turnover > 0.05:
                dup += 20.0
            elif normalized_turnover >= 0.02:
                dup += 10.0

        hits = int(bool(is_limit_up)) + int(bool(gap_breakaway))
        if hits >= 2:
            dup += 15.0
        elif hits == 1:
            dup += 10.0

        if above_ma100:
            dup += 5.0

        return round(max(0.0, dup) * 0.15, 6)

    def calculate_extreme_strength_score(
        self,
        above_ma100: bool,
        gap_breakaway: bool,
        pattern_123_low_trendline: bool,
        is_limit_up: bool,
        bottom_divergence_double_breakout: bool,
        theme_heat_score: float,
        leader_score: float,
        volume_ratio: float,
        turnover_rate: Optional[float],
        circ_mv: Optional[float],
        breakout_ratio: float,
        pattern_123_watchlist: bool = False,
        timing_penalty: float = 0.0,
    ) -> float:
        """
        Calculate total extreme strength score.
        Components:
        - base_score: 0-20
        - signal_bonus: 0-49
        - auxiliary_bonus: 0-53
        - timing_penalty: <= 0 (阶段 A3 接入，默认 0)
        Total: 0-122 (timing_penalty=0 时)

        当前实现委托给 calculate_layered_scores，用于保持总分语义与分层视图一致。
        """
        return self.calculate_layered_scores(
            above_ma100=above_ma100,
            gap_breakaway=gap_breakaway,
            pattern_123_low_trendline=pattern_123_low_trendline,
            is_limit_up=is_limit_up,
            bottom_divergence_double_breakout=bottom_divergence_double_breakout,
            theme_heat_score=theme_heat_score,
            leader_score=leader_score,
            volume_ratio=volume_ratio,
            turnover_rate=turnover_rate,
            circ_mv=circ_mv,
            breakout_ratio=breakout_ratio,
            pattern_123_watchlist=pattern_123_watchlist,
            timing_penalty=timing_penalty,
        ).total_score

    @staticmethod
    def _normalize_turnover_rate(turnover_rate: Optional[float]) -> Optional[float]:
        if turnover_rate is None:
            return None
        if turnover_rate > 1:
            return turnover_rate / 100.0
        return turnover_rate

    def is_selected(self, extreme_strength_score: float) -> bool:
        """Check if stock is selected (score >= 80)."""
        return extreme_strength_score >= self.SELECTED_THRESHOLD

    def is_watchlist(self, extreme_strength_score: float) -> bool:
        """Check if stock is in watchlist (60 <= score < 80)."""
        return (
            self.WATCHLIST_THRESHOLD_MIN
            <= extreme_strength_score
            < self.WATCHLIST_THRESHOLD_MAX
        )
