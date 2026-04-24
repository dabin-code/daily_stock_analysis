# -*- coding: utf-8 -*-
"""
L4 买点成熟度评估 — 基于 detector 状态机输出评估 EntryMaturity。
"""

from __future__ import annotations

from src.schemas.trading_types import EntryMaturity, SetupType


class EntryMaturityAssessor:

    def assess(self, setup_type: SetupType, factor_snapshot: dict) -> EntryMaturity:
        if setup_type == SetupType.NONE:
            return EntryMaturity.LOW

        handler = _HANDLERS.get(setup_type, _default_assess)
        return handler(factor_snapshot)


def _assess_bottom_divergence(fs: dict) -> EntryMaturity:
    """底背离双突破 → 成熟度。

    状态映射对齐 Notion《各形态买卖点识别手册》：
    - ``confirmed``：双突破同步 → HIGH
    - ``late_or_weak``：两条线都突破但同步性弱（间隔 > 3 bars）→ MEDIUM，
      手册认可此买点成立但明确其成熟度略逊于同步双突破
    - ``structure_ready``：仅单边突破 → MEDIUM
    - 其它（``divergence_only`` / ``rejected``）→ LOW
    """
    state = fs.get("bottom_divergence_state", "")
    if state == "confirmed":
        return EntryMaturity.HIGH
    if state in ("late_or_weak", "structure_ready"):
        return EntryMaturity.MEDIUM
    return EntryMaturity.LOW


def _assess_low123(fs: dict) -> EntryMaturity:
    state = fs.get("pattern_123_state", "")
    strength = fs.get("pattern_123_signal_strength", 0.0)
    if state == "breakout_ready" and strength >= 0.5:
        return EntryMaturity.HIGH
    if state == "breakout_ready":
        return EntryMaturity.MEDIUM
    if state == "watching":
        return EntryMaturity.MEDIUM
    return EntryMaturity.LOW


def _assess_trend_breakout(fs: dict) -> EntryMaturity:
    # Prefer the new "bars since real MA100 crossing" field.  -1 (or missing)
    # means no clean crossing was detected → fall back to the legacy
    # consecutive-above counter which is still informative for older snapshots.
    bars_since = fs.get("ma100_bars_since_breakout")
    if isinstance(bars_since, (int, float)) and bars_since >= 0:
        if bars_since <= 4:
            return EntryMaturity.HIGH
        if bars_since <= 9:
            return EntryMaturity.MEDIUM
        return EntryMaturity.LOW

    days = fs.get("ma100_breakout_days", 999)
    if days <= 5:
        return EntryMaturity.HIGH
    if days <= 10:
        return EntryMaturity.MEDIUM
    return EntryMaturity.LOW


def _assess_trend_pullback(fs: dict) -> EntryMaturity:
    """MA5/MA10 缩量回踩企稳 → 成熟度评估。

    优先消费 ShrinkPullbackDetector 产出的 ``shrink_pullback_state`` 和
    ``shrink_pullback_support_ma``；当新字段缺失时回退到旧 ``pullback_ma20``
    字段以兼容历史 snapshot。
    """
    state = fs.get("shrink_pullback_state")
    support = fs.get("shrink_pullback_support_ma")
    if state == "confirmed":
        return EntryMaturity.HIGH if support == "MA5" else EntryMaturity.MEDIUM
    if state == "stabilizing":
        return EntryMaturity.MEDIUM
    if state in ("touch_only", "rejected"):
        return EntryMaturity.LOW
    if fs.get("pullback_ma20", False):
        return EntryMaturity.MEDIUM
    return EntryMaturity.LOW


def _assess_gap_breakout(fs: dict) -> EntryMaturity:
    """Map gap-style buy points to EntryMaturity.

    Routing priority (new sub-semantics first, legacy fallback last):
    1. ``retest_hold`` — highest maturity (second entry on validated support).
    2. ``breakaway_gap_strict`` — high maturity when the breakout is clean
       and exhaustion risk is absent, otherwise medium.
    3. ``continuation_gap`` — medium maturity (trend-follow, lower edge).
    4. Legacy ``gap_breakaway`` + optional ``is_limit_up`` combo — medium/high.
    """
    if fs.get("retest_hold"):
        return EntryMaturity.HIGH

    exhaustion = fs.get("gap_exhaustion_risk_level", "none")
    if fs.get("breakaway_gap_strict"):
        return EntryMaturity.HIGH if exhaustion == "none" else EntryMaturity.MEDIUM
    if fs.get("continuation_gap"):
        return EntryMaturity.MEDIUM

    gap = fs.get("gap_breakaway", False)
    limit_up = fs.get("is_limit_up", False)
    if gap and limit_up:
        return EntryMaturity.HIGH
    if gap:
        return EntryMaturity.MEDIUM
    return EntryMaturity.LOW


def _assess_limitup_structure(fs: dict) -> EntryMaturity:
    """Map limit-up structural breakouts to EntryMaturity.

    Routing priority:
    1. ``retest_hold`` — highest maturity (post-limit-up support retest).
    2. ``limitup_structure_breakout`` + no high-acceleration risk — high.
    3. Legacy ``is_limit_up`` without structure — medium.
    """
    if fs.get("retest_hold"):
        return EntryMaturity.HIGH
    if fs.get("limitup_structure_breakout") and not fs.get(
        "limitup_high_acceleration_risk", False
    ):
        return EntryMaturity.HIGH
    if fs.get("is_limit_up"):
        # Limit-up without a structural break is still actionable but less
        # mature — downgrade relative to the clean structural case.
        return EntryMaturity.MEDIUM
    return EntryMaturity.LOW


def _default_assess(fs: dict) -> EntryMaturity:
    return EntryMaturity.LOW


_HANDLERS = {
    SetupType.BOTTOM_DIVERGENCE_BREAKOUT: _assess_bottom_divergence,
    SetupType.LOW123_BREAKOUT: _assess_low123,
    SetupType.TREND_BREAKOUT: _assess_trend_breakout,
    SetupType.TREND_PULLBACK: _assess_trend_pullback,
    SetupType.GAP_BREAKOUT: _assess_gap_breakout,
    SetupType.LIMITUP_STRUCTURE: _assess_limitup_structure,
}
