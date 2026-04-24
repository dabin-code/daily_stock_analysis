# -*- coding: utf-8 -*-
"""
L3 candidate pool classification simplified for leader-stock workflow.

Rule:
- Only main/secondary theme limit-up stocks can enter leader_pool.
- Other stocks stay out of leader_pool regardless of legacy strength scores.
- Stocks flagged as ``extended_do_not_chase`` by the timing assessor are
  downgraded by one tier (leader_pool -> focus_list) as an opt-in safety rail;
  when ``stage_label`` is ``None`` the legacy behaviour is preserved.
"""

from __future__ import annotations

from typing import Optional

from src.schemas.trading_types import CandidatePoolLevel, MarketRegime, ThemePosition

_LEADER_ELIGIBLE_THEMES = frozenset({
    ThemePosition.MAIN_THEME,
    ThemePosition.SECONDARY_THEME,
})

_WATCHLIST_ONLY_THEMES = frozenset({
    ThemePosition.NON_THEME,
    ThemePosition.FADING_THEME,
})

# A3 语义：当 ExtremeStrengthTimingAssessor 判定为 "已走远勿追" 时触发一级降级
EXTENDED_DO_NOT_CHASE = "extended_do_not_chase"


class CandidatePoolClassifier:

    def classify(
        self,
        leader_score: float,
        extreme_strength_score: float,
        theme_position: ThemePosition,
        market_regime: Optional[MarketRegime] = None,
        is_limit_up: bool = False,
        stage_label: Optional[str] = None,
    ) -> CandidatePoolLevel:
        del leader_score, extreme_strength_score

        if market_regime == MarketRegime.STAND_ASIDE:
            return CandidatePoolLevel.WATCHLIST

        if theme_position in _WATCHLIST_ONLY_THEMES:
            return CandidatePoolLevel.WATCHLIST

        if theme_position in _LEADER_ELIGIBLE_THEMES and is_limit_up:
            base_level = CandidatePoolLevel.LEADER_POOL
        elif theme_position == ThemePosition.FOLLOWER_THEME and is_limit_up:
            base_level = CandidatePoolLevel.FOCUS_LIST
        elif theme_position in _LEADER_ELIGIBLE_THEMES:
            base_level = CandidatePoolLevel.FOCUS_LIST
        else:
            base_level = CandidatePoolLevel.WATCHLIST

        # stage_label 为 None 时完全保留旧行为；仅当明确标记为 "已走远勿追"
        # 且当前档位为 LEADER_POOL 时，降级到 FOCUS_LIST，避免扎堆追高。
        if stage_label == EXTENDED_DO_NOT_CHASE and base_level == CandidatePoolLevel.LEADER_POOL:
            return CandidatePoolLevel.FOCUS_LIST

        return base_level
