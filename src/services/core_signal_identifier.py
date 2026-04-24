# -*- coding: utf-8 -*-
"""Core signal identifier service for extreme strength screening.

阶段 A4 调整：
- 引入 ``SignalKind`` 维度，对齐 ``extreme_strength_combo`` 的"池子层"语义
- 不再强制"跳空/涨停"高于"低位 123 / 底背离双突破"：
  结构性低位入场（structure_low_entry）排序优先级高于
  动量突破（momentum_breakout）与动量追涨（momentum_chase）
- 旧字段 ``core_signal`` / ``core_signal_score`` / ``bonus_signals`` /
  ``bonus_score`` 保持向后兼容（hot_theme_screener 等下游继续使用）
- 新增 ``classify_signals`` 作为统一入口，返回 ``primary_signal`` /
  ``signal_kind`` / ``all_signals`` 等新字段；供 enricher / UI 使用
"""

from enum import Enum
from typing import Any, Dict, List, Optional


class SignalKind(str, Enum):
    """信号类型（用于阶段/风格分层）。"""

    # 结构性低位入场（风险回报最优）
    STRUCTURE_LOW_ENTRY = "structure_low_entry"

    # 动量突破（信号当下，阶段感清晰）
    MOMENTUM_BREAKOUT = "momentum_breakout"

    # 动量追涨（强度高，但可能偏晚）
    MOMENTUM_CHASE = "momentum_chase"

    # 无信号
    NONE = "none"


# Core signal scores（保留旧权重，hot_theme_screener 的 total_score 仍依赖）
CORE_SIGNAL_SCORES: Dict[str, int] = {
    "跳空涨停": 15,
    "缺口突破MA100": 12,
    "涨停": 10,
}

# Bonus signal scores（保留旧权重）
BONUS_SIGNAL_SCORES: Dict[str, int] = {
    "低位123结构": 12,
    "底背离双突破": 12,
}

# Signal → SignalKind 映射（单一来源，供所有分类逻辑复用）
SIGNAL_KIND_MAP: Dict[str, SignalKind] = {
    "跳空涨停": SignalKind.MOMENTUM_CHASE,
    "涨停": SignalKind.MOMENTUM_CHASE,
    "缺口突破MA100": SignalKind.MOMENTUM_BREAKOUT,
    "低位123结构": SignalKind.STRUCTURE_LOW_ENTRY,
    "底背离双突破": SignalKind.STRUCTURE_LOW_ENTRY,
}

# SignalKind 排序优先级：数值越小越优先被选为 primary_signal。
# 结构性低位 > 动量突破 > 动量追涨，避免"跳空涨停自动压住低位123"。
_KIND_PRIORITY: Dict[SignalKind, int] = {
    SignalKind.STRUCTURE_LOW_ENTRY: 0,
    SignalKind.MOMENTUM_BREAKOUT: 1,
    SignalKind.MOMENTUM_CHASE: 2,
    SignalKind.NONE: 99,
}


class CoreSignalIdentifier:
    """Identify core technical signals and calculate scores."""

    def identify_core_signal(
        self,
        has_gap: bool = False,
        has_limit_up: bool = False,
        has_gap_breakout_ma100: bool = False,
    ) -> Dict[str, Any]:
        """Identify the strongest gap/limitup core signal.

        保留旧优先级（跳空涨停 > 缺口突破MA100 > 涨停），并额外返回
        ``signal_kind``。下游若希望跨类别（含结构信号）重选 primary，
        请调用 :meth:`classify_signals`。

        Args:
            has_gap: Whether stock has a gap up
            has_limit_up: Whether stock hit limit up
            has_gap_breakout_ma100: Whether stock has a gap breakout above MA100

        Returns:
            Dict with ``core_signal``, ``core_signal_score``, ``signal_kind``,
            ``hit_reasons``.
        """
        hit_reasons: List[str] = []

        if has_gap and has_limit_up:
            hit_reasons.append("跳空涨停（缺口+涨停共振）")
            return self._core_payload("跳空涨停", hit_reasons)

        if has_gap_breakout_ma100:
            hit_reasons.append("缺口突破MA100均线")
            return self._core_payload("缺口突破MA100", hit_reasons)

        if has_limit_up:
            hit_reasons.append("涨停")
            return self._core_payload("涨停", hit_reasons)

        return {
            "core_signal": None,
            "core_signal_score": 0,
            "signal_kind": SignalKind.NONE.value,
            "hit_reasons": hit_reasons,
        }

    def identify_bonus_signals(
        self,
        has_low_123_breakout: bool = False,
        has_bottom_divergence: bool = False,
    ) -> Dict[str, Any]:
        """Identify bonus signals (结构性低位入场).

        Args:
            has_low_123_breakout: Whether stock shows low 123 structure breakout
            has_bottom_divergence: Whether stock shows bottom divergence double breakout

        Returns:
            Dict with ``bonus_signals``, ``bonus_score``, ``signal_kinds``
            (per-signal kind list)，``dominant_kind`` (占主导的 kind)，
            ``hit_reasons``.
        """
        bonus_signals: List[str] = []
        signal_kinds: List[str] = []
        bonus_score = 0
        hit_reasons: List[str] = []

        if has_low_123_breakout:
            bonus_signals.append("低位123结构")
            signal_kinds.append(SIGNAL_KIND_MAP["低位123结构"].value)
            bonus_score += BONUS_SIGNAL_SCORES["低位123结构"]
            hit_reasons.append("低位123结构+涨停突破高点2")

        if has_bottom_divergence:
            bonus_signals.append("底背离双突破")
            signal_kinds.append(SIGNAL_KIND_MAP["底背离双突破"].value)
            bonus_score += BONUS_SIGNAL_SCORES["底背离双突破"]
            hit_reasons.append("底背离双突破")

        dominant_kind = (
            SignalKind.STRUCTURE_LOW_ENTRY.value if bonus_signals else SignalKind.NONE.value
        )

        return {
            "bonus_signals": bonus_signals,
            "bonus_score": bonus_score,
            "signal_kinds": signal_kinds,
            "dominant_kind": dominant_kind,
            "hit_reasons": hit_reasons,
        }

    def classify_signals(
        self,
        *,
        has_gap: bool = False,
        has_limit_up: bool = False,
        has_gap_breakout_ma100: bool = False,
        has_low_123_breakout: bool = False,
        has_bottom_divergence: bool = False,
    ) -> Dict[str, Any]:
        """统一分类入口：跨 core/bonus 根据 ``signal_kind`` 选择 primary_signal。

        优先级：``structure_low_entry`` > ``momentum_breakout`` > ``momentum_chase``。
        同一类别内按单信号分数降序，再按信号名稳定排序，保证可复现。

        返回字段：
        - ``primary_signal`` / ``primary_signal_kind``：跨类别重选后的主信号
        - ``all_signals``：所有命中信号列表，每项 ``{name, kind, score}``
        - ``core_signal`` / ``core_signal_score``：兼容旧字段（仅 gap/limitup）
        - ``bonus_signals`` / ``bonus_score``：兼容旧字段（仅 结构信号）
        - ``hit_reasons``：合并后的可读原因
        """
        core = self.identify_core_signal(
            has_gap=has_gap,
            has_limit_up=has_limit_up,
            has_gap_breakout_ma100=has_gap_breakout_ma100,
        )
        bonus = self.identify_bonus_signals(
            has_low_123_breakout=has_low_123_breakout,
            has_bottom_divergence=has_bottom_divergence,
        )

        all_signals: List[Dict[str, Any]] = []
        if core.get("core_signal"):
            name = core["core_signal"]
            all_signals.append(
                {
                    "name": name,
                    "kind": SIGNAL_KIND_MAP[name].value,
                    "score": CORE_SIGNAL_SCORES.get(name, 0),
                }
            )
        for name in bonus.get("bonus_signals", []):
            all_signals.append(
                {
                    "name": name,
                    "kind": SIGNAL_KIND_MAP[name].value,
                    "score": BONUS_SIGNAL_SCORES.get(name, 0),
                }
            )

        primary_signal, primary_signal_kind = self._pick_primary(all_signals)

        hit_reasons = list(core.get("hit_reasons", [])) + list(bonus.get("hit_reasons", []))

        return {
            "primary_signal": primary_signal,
            "primary_signal_kind": primary_signal_kind,
            "all_signals": all_signals,
            "core_signal": core.get("core_signal"),
            "core_signal_score": core.get("core_signal_score", 0),
            "core_signal_kind": core.get("signal_kind", SignalKind.NONE.value),
            "bonus_signals": bonus.get("bonus_signals", []),
            "bonus_score": bonus.get("bonus_score", 0),
            "bonus_signal_kinds": bonus.get("signal_kinds", []),
            "hit_reasons": hit_reasons,
        }

    def calculate_total_score(
        self,
        core_signal_score: int,
        bonus_score: int,
    ) -> int:
        """Calculate total score = core + bonus.

        Args:
            core_signal_score: Score from core signal
            bonus_score: Score from bonus signals

        Returns:
            Total score
        """
        return core_signal_score + bonus_score

    @staticmethod
    def _core_payload(signal_name: str, hit_reasons: List[str]) -> Dict[str, Any]:
        return {
            "core_signal": signal_name,
            "core_signal_score": CORE_SIGNAL_SCORES[signal_name],
            "signal_kind": SIGNAL_KIND_MAP[signal_name].value,
            "hit_reasons": hit_reasons,
        }

    @staticmethod
    def _pick_primary(
        all_signals: List[Dict[str, Any]],
    ) -> tuple[Optional[str], str]:
        """按 kind 优先级 → score 降序 → 名称稳定序 挑选 primary_signal。"""
        if not all_signals:
            return None, SignalKind.NONE.value
        sorted_signals = sorted(
            all_signals,
            key=lambda s: (
                _KIND_PRIORITY.get(SignalKind(s["kind"]), 99),
                -int(s.get("score", 0)),
                str(s["name"]),
            ),
        )
        top = sorted_signals[0]
        return str(top["name"]), str(top["kind"])
