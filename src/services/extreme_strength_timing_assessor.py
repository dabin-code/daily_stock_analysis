# -*- coding: utf-8 -*-
"""时机评估器 / Timing assessor for extreme_strength_combo stock pool.

阶段 A3 的目标：
- 为池子层策略 ``extreme_strength_combo`` 增加"时机惩罚"维度，避免
  "强但走远"的股票仍以高分排在候选前列
- 产出 ``stage_label`` 替代 ``entry_reason`` 的模糊描述，下游 UI/解释
  可以准确表达"仅观察 / 突破当日 / 回踩确认 / 已走远勿追"
- ``timing_penalty`` 仅产负分，最终会接入
  ``ExtremeStrengthScorer.calculate_layered_scores`` 的 ``timing_penalty`` 入参

关键设计：
- ``stage_label`` 是 ``str`` Enum，便于直接序列化到 snapshot 与前端 JSON
- 评估器保持纯函数（无外部依赖），方便单元测试
- 所有输入字段在 factor_service 里已经产出，本模块不新增采集逻辑
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List


class StageLabel(str, Enum):
    """交易阶段标签。"""

    # 未命中热点 / 池子层：不判定时机
    POOL_ONLY = "pool_only"

    # 热点命中但无具体入场信号：仅观察
    WATCH_ONLY = "watch_only"

    # 信号发生当天或前一日
    BREAKOUT_DAY = "breakout_day"

    # 突破后的回踩 / 确认阶段（2-3 根 K 线内）
    RETEST_ENTRY = "retest_entry"

    # 信号已过期或 extended 偏离过高：勿追
    EXTENDED_DO_NOT_CHASE = "extended_do_not_chase"


@dataclass(frozen=True)
class TimingAssessment:
    """时机评估结果。"""

    stage_label: StageLabel
    timing_penalty: float
    bars_since_primary_event: int
    extended_pct: float
    reasons: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "stage_label": self.stage_label.value,
            "timing_penalty": self.timing_penalty,
            "bars_since_primary_event": self.bars_since_primary_event,
            "extended_pct": self.extended_pct,
            "reasons": list(self.reasons),
        }


class ExtremeStrengthTimingAssessor:
    """时机评估器。

    语义分层：
    - 不是热点股                → pool_only, penalty=0（池子层不判定）
    - 是热点但无入场信号         → watch_only, penalty=0 或 extended 触发惩罚
    - 有信号且 bars_since ≤ 1   → breakout_day
    - 有信号且 bars_since ≤ 3   → retest_entry
    - 有信号但 bars_since > 3   → extended_do_not_chase, 按超期天数累加惩罚
    - extended_pct 超过强阈值    → 强制降级为 extended_do_not_chase
    """

    MAX_PENALTY: float = -30.0
    STALE_BARS_THRESHOLD: int = 3
    EXTENDED_PCT_SOFT: float = 15.0
    EXTENDED_PCT_HARD: float = 25.0
    STALE_PENALTY_PER_BAR: float = -3.0
    STALE_PENALTY_CAP: float = -15.0

    def assess(
        self,
        *,
        is_hot_theme: bool,
        has_entry_signal: bool,
        bars_since_primary_event: int,
        extended_pct: float,
    ) -> TimingAssessment:
        """核心评估接口（纯函数，无 snapshot 依赖）。"""
        if not is_hot_theme:
            return TimingAssessment(
                stage_label=StageLabel.POOL_ONLY,
                timing_penalty=0.0,
                bars_since_primary_event=bars_since_primary_event,
                extended_pct=extended_pct,
                reasons=["non_hot_theme"],
            )

        reasons: List[str] = []
        penalty = 0.0

        # extended 偏离惩罚（与 stage 无关，先累计）
        if extended_pct >= self.EXTENDED_PCT_HARD:
            penalty += -10.0
            reasons.append(f"extended_pct>={self.EXTENDED_PCT_HARD:.0f}")
        elif extended_pct >= self.EXTENDED_PCT_SOFT:
            penalty += -5.0
            reasons.append(f"extended_pct>={self.EXTENDED_PCT_SOFT:.0f}")

        if not has_entry_signal:
            # 无具体入场信号：仅观察；仅 extended 惩罚生效
            stage = StageLabel.WATCH_ONLY
            reasons.insert(0, "no_entry_signal")
            penalty = max(penalty, self.MAX_PENALTY)
            if extended_pct >= self.EXTENDED_PCT_HARD:
                stage = StageLabel.EXTENDED_DO_NOT_CHASE
                reasons.append("forced_extended_from_ma")
            return TimingAssessment(
                stage_label=stage,
                timing_penalty=penalty,
                bars_since_primary_event=bars_since_primary_event,
                extended_pct=extended_pct,
                reasons=reasons,
            )

        # 有入场信号：按 bars_since_primary_event 判定阶段
        if bars_since_primary_event < 0:
            stage = StageLabel.WATCH_ONLY
            reasons.append("no_event_bars")
        elif bars_since_primary_event <= 1:
            stage = StageLabel.BREAKOUT_DAY
            reasons.append(f"bars_since_event={bars_since_primary_event}")
        elif bars_since_primary_event <= self.STALE_BARS_THRESHOLD:
            stage = StageLabel.RETEST_ENTRY
            reasons.append(f"bars_since_event={bars_since_primary_event}")
        else:
            stage = StageLabel.EXTENDED_DO_NOT_CHASE
            over_bars = bars_since_primary_event - self.STALE_BARS_THRESHOLD
            stale_penalty = max(
                self.STALE_PENALTY_PER_BAR * over_bars,
                self.STALE_PENALTY_CAP,
            )
            penalty += stale_penalty
            reasons.append(
                f"bars_since_event={bars_since_primary_event}"
                f">STALE_{self.STALE_BARS_THRESHOLD}"
            )

        # extended 极高时强制降级
        if extended_pct >= self.EXTENDED_PCT_HARD and stage != StageLabel.EXTENDED_DO_NOT_CHASE:
            stage = StageLabel.EXTENDED_DO_NOT_CHASE
            reasons.append("forced_extended_from_ma")

        penalty = max(penalty, self.MAX_PENALTY)

        return TimingAssessment(
            stage_label=stage,
            timing_penalty=penalty,
            bars_since_primary_event=bars_since_primary_event,
            extended_pct=extended_pct,
            reasons=reasons,
        )

    def assess_from_snapshot(
        self,
        snapshot: Dict[str, Any],
        *,
        is_hot_theme: bool,
    ) -> TimingAssessment:
        """从 factor_snapshot 抽取关键时机字段并评估。"""
        has_entry_signal = bool(
            snapshot.get("gap_breakaway")
            or snapshot.get("is_limit_up")
            or snapshot.get("pattern_123_low_trendline")
            or snapshot.get("bottom_divergence_double_breakout")
        )
        bars_since_primary_event = self._extract_bars_since_primary_event(snapshot)
        extended_pct = self._extract_extended_pct(snapshot)
        return self.assess(
            is_hot_theme=is_hot_theme,
            has_entry_signal=has_entry_signal,
            bars_since_primary_event=bars_since_primary_event,
            extended_pct=extended_pct,
        )

    @staticmethod
    def _extract_bars_since_primary_event(snapshot: Dict[str, Any]) -> int:
        """取 gap / limit_up / ma100 三类事件 bars 的最小非负值，-1 表示均无事件。"""
        candidates: List[int] = []
        for key in (
            "bars_since_event",
            "bars_since_breakaway_gap",
            "bars_since_limitup_structure_breakout",
            "ma100_bars_since_breakout",
        ):
            val = snapshot.get(key)
            if val is None:
                continue
            try:
                iv = int(val)
            except (TypeError, ValueError):
                continue
            if iv >= 0:
                candidates.append(iv)
        return min(candidates) if candidates else -1

    @staticmethod
    def _extract_extended_pct(snapshot: Dict[str, Any]) -> float:
        """仅当股价站在 MA100 之上时才计算 extended 幅度；否则返回 0。"""
        raw = snapshot.get("ma100_distance_pct")
        if raw is None:
            return 0.0
        try:
            pct = float(raw)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, pct)
