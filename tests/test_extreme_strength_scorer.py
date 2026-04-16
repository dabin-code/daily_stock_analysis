# -*- coding: utf-8 -*-
"""Unit tests for ExtremeStrengthScorer."""

import unittest
from src.services.extreme_strength_scorer import ExtremeStrengthScorer


class ExtremeStrengthScorerTestCase(unittest.TestCase):
    """Test ExtremeStrengthScorer."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.scorer = ExtremeStrengthScorer()

    def test_base_score_above_ma100(self) -> None:
        """Test base score when above MA100."""
        score = self.scorer.calculate_base_score(above_ma100=True)
        self.assertEqual(score, 20)

    def test_base_score_below_ma100(self) -> None:
        """Test base score when below MA100."""
        score = self.scorer.calculate_base_score(above_ma100=False)
        self.assertEqual(score, 0)

    def test_signal_bonus_all_signals(self) -> None:
        """Test signal bonus with all signals present."""
        score = self.scorer.calculate_signal_bonus(
            gap_breakaway=True,
            pattern_123_low_trendline=True,
            is_limit_up=True,
            bottom_divergence_double_breakout=True,
        )
        # 15 + 12 + 10 + 12 = 49
        self.assertEqual(score, 49)

    def test_signal_bonus_no_signals(self) -> None:
        """Test signal bonus with no signals."""
        score = self.scorer.calculate_signal_bonus(
            gap_breakaway=False,
            pattern_123_low_trendline=False,
            is_limit_up=False,
            bottom_divergence_double_breakout=False,
        )
        self.assertEqual(score, 0)

    def test_signal_bonus_partial_signals(self) -> None:
        """Test signal bonus with partial signals."""
        score = self.scorer.calculate_signal_bonus(
            gap_breakaway=True,
            pattern_123_low_trendline=True,
            is_limit_up=False,
            bottom_divergence_double_breakout=False,
        )
        # 15 + 12 = 27
        self.assertEqual(score, 27)

    def test_signal_bonus_watchlist_is_weaker_than_breakout_ready(self) -> None:
        """watching 状态应加分，但弱于 breakout_ready。"""
        watch_score = self.scorer.calculate_signal_bonus(
            gap_breakaway=False,
            pattern_123_low_trendline=False,
            is_limit_up=False,
            bottom_divergence_double_breakout=False,
            pattern_123_watchlist=True,
        )
        breakout_score = self.scorer.calculate_signal_bonus(
            gap_breakaway=False,
            pattern_123_low_trendline=True,
            is_limit_up=False,
            bottom_divergence_double_breakout=False,
            pattern_123_watchlist=False,
        )
        self.assertGreater(watch_score, 0)
        self.assertLess(watch_score, breakout_score)

    def test_auxiliary_bonus_full(self) -> None:
        """Test auxiliary bonus at maximum."""
        score = self.scorer.calculate_auxiliary_bonus(
            theme_heat_score=100.0,
            leader_score=100,
            volume_ratio=2.0,
            turnover_rate=0.10,
            circ_mv=30_000_000_000,
            breakout_ratio=2.0,
        )
        # 10 + 15 + 8 + 6 + 6 + 8 = 53
        self.assertEqual(score, 53)

    def test_auxiliary_bonus_zero(self) -> None:
        """Test auxiliary bonus at minimum."""
        score = self.scorer.calculate_auxiliary_bonus(
            theme_heat_score=0.0,
            leader_score=0,
            volume_ratio=0.5,
            turnover_rate=0.001,
            circ_mv=200_000_000_000,
            breakout_ratio=0.5,
        )
        # Small bonus from turnover_rate: 0.001 * 60 = 0.06
        self.assertAlmostEqual(score, 0.06, places=2)

    def test_auxiliary_bonus_partial(self) -> None:
        """Test auxiliary bonus with partial factors."""
        score = self.scorer.calculate_auxiliary_bonus(
            theme_heat_score=50.0,
            leader_score=50,
            volume_ratio=1.5,
            turnover_rate=0.05,
            circ_mv=75_000_000_000,
            breakout_ratio=1.5,
        )
        # 5 + 7.5 + 4 + 3 + 3 + 4 = 26.5
        self.assertEqual(score, 26.5)

    def test_calculate_extreme_strength_score_full(self) -> None:
        """Test extreme strength score with all factors at maximum."""
        score = self.scorer.calculate_extreme_strength_score(
            above_ma100=True,
            gap_breakaway=True,
            pattern_123_low_trendline=True,
            is_limit_up=True,
            bottom_divergence_double_breakout=True,
            theme_heat_score=100.0,
            leader_score=100,
            volume_ratio=2.0,
            turnover_rate=0.10,
            circ_mv=30_000_000_000,
            breakout_ratio=2.0,
        )
        # base: 20, signals: 49, auxiliary: 53 = 122
        self.assertEqual(score, 122)

    def test_calculate_extreme_strength_score_zero(self) -> None:
        """Test extreme strength score with all factors at minimum."""
        score = self.scorer.calculate_extreme_strength_score(
            above_ma100=False,
            gap_breakaway=False,
            pattern_123_low_trendline=False,
            is_limit_up=False,
            bottom_divergence_double_breakout=False,
            theme_heat_score=0.0,
            leader_score=0,
            volume_ratio=0.5,
            turnover_rate=0.001,
            circ_mv=200_000_000_000,
            breakout_ratio=0.5,
        )
        # Small bonus from turnover_rate: 0.001 * 60 = 0.06
        self.assertAlmostEqual(score, 0.06, places=2)

    def test_calculate_extreme_strength_score_partial(self) -> None:
        """Test extreme strength score with partial factors."""
        score = self.scorer.calculate_extreme_strength_score(
            above_ma100=True,
            gap_breakaway=True,
            pattern_123_low_trendline=False,
            is_limit_up=True,
            bottom_divergence_double_breakout=False,
            theme_heat_score=75.0,
            leader_score=70,
            volume_ratio=1.5,
            turnover_rate=0.05,
            circ_mv=75_000_000_000,
            breakout_ratio=1.5,
        )
        # base: 20, signals: 25, auxiliary: ~20 = ~65
        # Actual: 20 + 25 + (7.5 + 10.5 + 4 + 3 + 3 + 4) = 77
        self.assertGreater(score, 70)
        self.assertLess(score, 85)

    def test_is_selected_above_threshold(self) -> None:
        """Test is_selected returns True when score >= 80."""
        result = self.scorer.is_selected(85)
        self.assertTrue(result)

    def test_is_selected_at_threshold(self) -> None:
        """Test is_selected returns True when score == 80."""
        result = self.scorer.is_selected(80)
        self.assertTrue(result)

    def test_is_selected_below_threshold(self) -> None:
        """Test is_selected returns False when score < 80."""
        result = self.scorer.is_selected(79)
        self.assertFalse(result)

    def test_is_watchlist_in_range(self) -> None:
        """Test is_watchlist returns True when 60 <= score < 80."""
        result = self.scorer.is_watchlist(70)
        self.assertTrue(result)

    def test_is_watchlist_below_range(self) -> None:
        """Test is_watchlist returns False when score < 60."""
        result = self.scorer.is_watchlist(59)
        self.assertFalse(result)

    def test_is_watchlist_above_range(self) -> None:
        """Test is_watchlist returns False when score >= 80."""
        result = self.scorer.is_watchlist(80)
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
