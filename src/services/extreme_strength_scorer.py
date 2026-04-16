# -*- coding: utf-8 -*-
"""Extreme strength scorer for hot theme stocks."""

from typing import Optional


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
    ) -> float:
        """
        Calculate total extreme strength score.
        Components:
        - base_score: 0-20
        - signal_bonus: 0-49
        - auxiliary_bonus: 0-53
        Total: 0-122
        """
        base = self.calculate_base_score(above_ma100)
        signals = self.calculate_signal_bonus(
            gap_breakaway=gap_breakaway,
            pattern_123_low_trendline=pattern_123_low_trendline,
            is_limit_up=is_limit_up,
            bottom_divergence_double_breakout=bottom_divergence_double_breakout,
            pattern_123_watchlist=pattern_123_watchlist,
        )
        auxiliary = self.calculate_auxiliary_bonus(
            theme_heat_score=theme_heat_score,
            leader_score=leader_score,
            volume_ratio=volume_ratio,
            turnover_rate=turnover_rate,
            circ_mv=circ_mv,
            breakout_ratio=breakout_ratio,
        )
        return base + signals + auxiliary

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
