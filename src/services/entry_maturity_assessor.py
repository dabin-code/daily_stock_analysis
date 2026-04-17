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
    state = fs.get("bottom_divergence_state", "")
    if state == "confirmed":
        return EntryMaturity.HIGH
    if state == "structure_ready":
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
    pullback_ma20 = fs.get("pullback_ma20", False)
    if pullback_ma20:
        return EntryMaturity.MEDIUM
    return EntryMaturity.LOW


def _assess_gap_breakout(fs: dict) -> EntryMaturity:
    gap = fs.get("gap_breakaway", False)
    limit_up = fs.get("is_limit_up", False)
    if gap and limit_up:
        return EntryMaturity.HIGH
    if gap:
        return EntryMaturity.MEDIUM
    return EntryMaturity.LOW


def _assess_limitup_structure(fs: dict) -> EntryMaturity:
    limit_up = fs.get("is_limit_up", False)
    if limit_up:
        return EntryMaturity.HIGH
    return EntryMaturity.MEDIUM


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
