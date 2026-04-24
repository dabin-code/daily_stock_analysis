# -*- coding: utf-8 -*-
"""Unit tests for ExtremeStrengthScorer."""

import unittest
from src.services.extreme_strength_scorer import (
    ExtremeStrengthScorer,
    LayeredExtremeStrengthScores,
)


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


class LayeredExtremeStrengthScoresTestCase(unittest.TestCase):
    """阶段 A1：分层评分结果验证。

    目标：
    - 分层拆分必须与旧 calculate_extreme_strength_score 总分数值等价
    - 四个桶（theme_pool / leadership / entry_signal / timing_penalty）之和 == total_score
    - timing_penalty 非零时能按预期下拉总分
    - breakdown 每一项都可逐项溯源
    """

    def setUp(self) -> None:
        self.scorer = ExtremeStrengthScorer()
        self.full_kwargs = dict(
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

    def test_returns_dataclass_with_required_fields(self) -> None:
        """分层评分必须返回 LayeredExtremeStrengthScores 且字段齐备。"""
        layered = self.scorer.calculate_layered_scores(**self.full_kwargs)
        self.assertIsInstance(layered, LayeredExtremeStrengthScores)
        self.assertTrue(hasattr(layered, "theme_pool_score"))
        self.assertTrue(hasattr(layered, "leadership_score"))
        self.assertTrue(hasattr(layered, "entry_signal_score"))
        self.assertTrue(hasattr(layered, "timing_penalty"))
        self.assertTrue(hasattr(layered, "total_score"))
        self.assertTrue(hasattr(layered, "breakdown"))

    def test_layers_sum_to_total_full(self) -> None:
        """满分场景：四桶之和 == total_score == 122。"""
        layered = self.scorer.calculate_layered_scores(**self.full_kwargs)
        bucket_sum = (
            layered.theme_pool_score
            + layered.leadership_score
            + layered.entry_signal_score
            + layered.timing_penalty
        )
        self.assertAlmostEqual(bucket_sum, layered.total_score, places=6)
        self.assertEqual(layered.total_score, 122)

    def test_layers_sum_to_total_partial(self) -> None:
        """部分命中场景：四桶之和仍然 == total_score。"""
        layered = self.scorer.calculate_layered_scores(
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
        bucket_sum = (
            layered.theme_pool_score
            + layered.leadership_score
            + layered.entry_signal_score
            + layered.timing_penalty
        )
        self.assertAlmostEqual(bucket_sum, layered.total_score, places=6)

    def test_total_equals_legacy_calculate(self) -> None:
        """分层 total_score 必须与旧 calculate_extreme_strength_score 严格一致。"""
        legacy_total = self.scorer.calculate_extreme_strength_score(**self.full_kwargs)
        layered = self.scorer.calculate_layered_scores(**self.full_kwargs)
        self.assertAlmostEqual(legacy_total, layered.total_score, places=6)

    def test_theme_pool_score_only_from_theme_heat(self) -> None:
        """theme_pool_score 仅由 theme_heat_score 决定，最大 10。"""
        layered = self.scorer.calculate_layered_scores(**self.full_kwargs)
        self.assertAlmostEqual(layered.theme_pool_score, 10.0, places=6)

        kwargs_no_heat = dict(self.full_kwargs)
        kwargs_no_heat["theme_heat_score"] = 0.0
        layered_no_heat = self.scorer.calculate_layered_scores(**kwargs_no_heat)
        self.assertEqual(layered_no_heat.theme_pool_score, 0.0)

    def test_entry_signal_score_contains_base_plus_signal(self) -> None:
        """entry_signal_score == base(20) + signal_bonus(49)，与 above_ma100/信号开关联动。"""
        layered = self.scorer.calculate_layered_scores(**self.full_kwargs)
        self.assertEqual(layered.entry_signal_score, 69.0)

        kwargs_flat = dict(self.full_kwargs)
        kwargs_flat.update(
            above_ma100=False,
            gap_breakaway=False,
            pattern_123_low_trendline=False,
            is_limit_up=False,
            bottom_divergence_double_breakout=False,
        )
        layered_flat = self.scorer.calculate_layered_scores(**kwargs_flat)
        self.assertEqual(layered_flat.entry_signal_score, 0.0)

    def test_leadership_score_bundles_leader_and_activity(self) -> None:
        """leadership_score 包含 leader_score/volume/turnover/circ_mv/breakout 五项活跃度贡献。"""
        layered = self.scorer.calculate_layered_scores(**self.full_kwargs)
        self.assertAlmostEqual(layered.leadership_score, 43.0, places=6)

        breakdown = layered.breakdown
        bundle_sum = (
            breakdown["leader_contribution"]
            + breakdown["volume_contribution"]
            + breakdown["turnover_contribution"]
            + breakdown["circ_mv_contribution"]
            + breakdown["breakout_contribution"]
        )
        self.assertAlmostEqual(bundle_sum, layered.leadership_score, places=6)

    def test_timing_penalty_defaults_to_zero(self) -> None:
        """未传 timing_penalty 时默认 0（阶段 A3 接入）。"""
        layered = self.scorer.calculate_layered_scores(**self.full_kwargs)
        self.assertEqual(layered.timing_penalty, 0.0)

    def test_timing_penalty_reduces_total(self) -> None:
        """timing_penalty 为负时总分应相应下降。"""
        layered_no_penalty = self.scorer.calculate_layered_scores(**self.full_kwargs)
        layered_with_penalty = self.scorer.calculate_layered_scores(
            timing_penalty=-15.0,
            **self.full_kwargs,
        )
        self.assertAlmostEqual(
            layered_with_penalty.total_score,
            layered_no_penalty.total_score - 15.0,
            places=6,
        )
        self.assertEqual(layered_with_penalty.timing_penalty, -15.0)

    def test_positive_timing_penalty_coerced_to_zero(self) -> None:
        """正数 timing_penalty 应被安全归零，不能反向拉高总分。"""
        layered = self.scorer.calculate_layered_scores(
            timing_penalty=25.0,
            **self.full_kwargs,
        )
        self.assertEqual(layered.timing_penalty, 0.0)

    def test_breakdown_contains_all_subcomponents(self) -> None:
        """breakdown 必须暴露 base/signal/theme_heat/leader/volume/turnover/circ_mv/breakout
        八个可溯源子项，以及 A7 新增的 leader_double_count 估算值。"""
        layered = self.scorer.calculate_layered_scores(**self.full_kwargs)
        expected_keys = {
            "base_score",
            "signal_bonus",
            "theme_heat_contribution",
            "leader_contribution",
            "volume_contribution",
            "turnover_contribution",
            "circ_mv_contribution",
            "breakout_contribution",
            # A7：leader_score 与其它桶之间可观测的重复加权估算值（已按 0.15 缩放）
            "leader_double_count",
        }
        self.assertEqual(set(layered.breakdown.keys()), expected_keys)

    def test_as_dict_serialization(self) -> None:
        """as_dict 应能序列化到纯字典，便于写入 snapshot。"""
        layered = self.scorer.calculate_layered_scores(**self.full_kwargs)
        payload = layered.as_dict()
        self.assertIn("total_score", payload)
        self.assertIn("breakdown", payload)
        self.assertIn("leader_double_count", payload)
        self.assertIn("deduplicated_total_score", payload)
        self.assertIsInstance(payload["breakdown"], dict)


class LeaderDoubleCountTestCase(unittest.TestCase):
    """阶段 A7：把 leader_score 与其它桶之间的可观测重复加权显式估算出来。

    契约：
    - 仅暴露为估算值 + 去重后的净总分，不修改 total_score 原始数值（向后兼容）
    - 重复项包含：small_circ_mv / turnover / breakout_strength(is_limit_up+gap_breakaway) /
      trend_strength(above_ma100 部分，保守下界) 共 4 项
    - 未传入 leader_score=0 时重复加权估算也应为 0（leader_score 没吃任何重复）
    - deduplicated_total_score == total_score - leader_double_count
    - leader_double_count 必须非负，且不会超过 leader_contribution
    """

    def setUp(self) -> None:
        self.scorer = ExtremeStrengthScorer()
        self.full_kwargs = dict(
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

    def test_full_overlap_equals_expected_estimate(self) -> None:
        """满命中场景下，重复加权估算 = (20+20+15+5) × 0.15 = 9.0。

        trend_strength 保守按 above_ma100 → 5 分下界处理（避免对 ma100_breakout_days
        过度推断），其余三项按 leader_score_calculator 的精确规则复刻。
        """
        layered = self.scorer.calculate_layered_scores(**self.full_kwargs)
        self.assertAlmostEqual(layered.leader_double_count, 9.0, places=6)

    def test_zero_leader_score_caps_double_count_to_zero(self) -> None:
        """leader_score=0 → leader_contribution=0 → leader_double_count 上限为 0。

        这是安全裁剪：重复加权只在 leader_score 实际"吃过"因子时才存在，
        若调用方传入的 scalar leader_score 为 0，代表该路径未使用 LeaderScoreCalculator，
        此时不能继续断言存在重复，避免 deduplicated_total_score 被错误下调。
        """
        kwargs = dict(self.full_kwargs)
        kwargs["leader_score"] = 0
        layered = self.scorer.calculate_layered_scores(**kwargs)
        self.assertEqual(layered.breakdown["leader_contribution"], 0.0)
        # 原始估算（按因子口径）是 9.0，但被 leader_contribution 裁到 0
        self.assertEqual(layered.leader_double_count, 0.0)
        self.assertAlmostEqual(
            layered.deduplicated_total_score, layered.total_score, places=6
        )

    def test_no_overlap_factors_yield_zero_double_count(self) -> None:
        """所有重复因子均关掉 → leader_double_count == 0。"""
        layered = self.scorer.calculate_layered_scores(
            above_ma100=False,
            gap_breakaway=False,
            pattern_123_low_trendline=False,
            is_limit_up=False,
            bottom_divergence_double_breakout=False,
            theme_heat_score=80.0,
            leader_score=50,
            volume_ratio=1.5,
            turnover_rate=0.01,
            circ_mv=200_000_000_000,
            breakout_ratio=1.2,
        )
        self.assertEqual(layered.leader_double_count, 0.0)
        self.assertAlmostEqual(
            layered.deduplicated_total_score, layered.total_score, places=6
        )

    def test_partial_overlap_from_small_cap_and_turnover_only(self) -> None:
        """只命中 small_circ_mv + turnover：重复估算 = (20+20)×0.15 = 6.0。"""
        layered = self.scorer.calculate_layered_scores(
            above_ma100=False,
            gap_breakaway=False,
            pattern_123_low_trendline=False,
            is_limit_up=False,
            bottom_divergence_double_breakout=False,
            theme_heat_score=50.0,
            leader_score=60,
            volume_ratio=1.0,
            turnover_rate=0.06,
            circ_mv=30_000_000_000,
            breakout_ratio=1.0,
        )
        self.assertAlmostEqual(layered.leader_double_count, 6.0, places=6)

    def test_deduplicated_equals_total_minus_double_count(self) -> None:
        """deduplicated_total_score 必须等于 total_score - leader_double_count。"""
        layered = self.scorer.calculate_layered_scores(**self.full_kwargs)
        self.assertAlmostEqual(
            layered.deduplicated_total_score,
            layered.total_score - layered.leader_double_count,
            places=6,
        )

    def test_double_count_never_exceeds_leader_contribution(self) -> None:
        """leader_double_count 是 leader_contribution 内部的子集，不应超出它。"""
        layered = self.scorer.calculate_layered_scores(**self.full_kwargs)
        self.assertLessEqual(
            layered.leader_double_count,
            layered.breakdown["leader_contribution"] + 1e-6,
        )

    def test_double_count_never_negative(self) -> None:
        """估算值必须 >= 0，不允许回溯减分。"""
        layered = self.scorer.calculate_layered_scores(
            above_ma100=False,
            gap_breakaway=False,
            pattern_123_low_trendline=False,
            is_limit_up=False,
            bottom_divergence_double_breakout=False,
            theme_heat_score=0.0,
            leader_score=0,
            volume_ratio=0.5,
            turnover_rate=0.0,
            circ_mv=None,
            breakout_ratio=0.5,
        )
        self.assertGreaterEqual(layered.leader_double_count, 0.0)

    def test_legacy_total_score_remains_unchanged(self) -> None:
        """关键向后兼容契约：total_score 依然等于旧 calculate_extreme_strength_score。"""
        legacy_total = self.scorer.calculate_extreme_strength_score(**self.full_kwargs)
        layered = self.scorer.calculate_layered_scores(**self.full_kwargs)
        self.assertAlmostEqual(legacy_total, layered.total_score, places=6)
        # 去重后的净总分比原总分低，但不影响 total_score / is_selected 判断
        self.assertLess(layered.deduplicated_total_score, layered.total_score)


if __name__ == "__main__":
    unittest.main()
