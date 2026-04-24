# -*- coding: utf-8 -*-
"""阶段 A3：ExtremeStrengthTimingAssessor 的单元测试。

锁定：
- 未命中热点 → pool_only, penalty=0
- 命中热点但无入场信号 → watch_only, penalty=0（除非 extended 过高）
- bars_since_primary_event ∈ [0, 1] → breakout_day
- bars_since_primary_event ∈ [2, 3] → retest_entry
- bars_since_primary_event > 3 → extended_do_not_chase，按超期天数累加惩罚
- extended_pct 超强阈值 → 强制降级为 extended_do_not_chase
- penalty 上限为 MAX_PENALTY
- assess_from_snapshot 从 factor_service 字段抽取输入
"""

from __future__ import annotations

import unittest

from src.services.extreme_strength_timing_assessor import (
    ExtremeStrengthTimingAssessor,
    StageLabel,
    TimingAssessment,
)


class AssessorPureLogicTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.assessor = ExtremeStrengthTimingAssessor()

    def test_non_hot_theme_is_pool_only(self) -> None:
        result = self.assessor.assess(
            is_hot_theme=False,
            has_entry_signal=True,
            bars_since_primary_event=0,
            extended_pct=10.0,
        )
        self.assertEqual(result.stage_label, StageLabel.POOL_ONLY)
        self.assertEqual(result.timing_penalty, 0.0)
        self.assertIn("non_hot_theme", result.reasons)

    def test_hot_without_signal_is_watch_only(self) -> None:
        result = self.assessor.assess(
            is_hot_theme=True,
            has_entry_signal=False,
            bars_since_primary_event=-1,
            extended_pct=0.0,
        )
        self.assertEqual(result.stage_label, StageLabel.WATCH_ONLY)
        self.assertEqual(result.timing_penalty, 0.0)
        self.assertIn("no_entry_signal", result.reasons)

    def test_breakout_day_zero_penalty(self) -> None:
        for bars in (0, 1):
            result = self.assessor.assess(
                is_hot_theme=True,
                has_entry_signal=True,
                bars_since_primary_event=bars,
                extended_pct=5.0,
            )
            self.assertEqual(result.stage_label, StageLabel.BREAKOUT_DAY)
            self.assertEqual(result.timing_penalty, 0.0)

    def test_retest_entry_zero_penalty(self) -> None:
        for bars in (2, 3):
            result = self.assessor.assess(
                is_hot_theme=True,
                has_entry_signal=True,
                bars_since_primary_event=bars,
                extended_pct=0.0,
            )
            self.assertEqual(result.stage_label, StageLabel.RETEST_ENTRY)
            self.assertEqual(result.timing_penalty, 0.0)

    def test_stale_bars_trigger_extended(self) -> None:
        result = self.assessor.assess(
            is_hot_theme=True,
            has_entry_signal=True,
            bars_since_primary_event=5,
            extended_pct=0.0,
        )
        self.assertEqual(result.stage_label, StageLabel.EXTENDED_DO_NOT_CHASE)
        # 超 2 根 → -3*2=-6
        self.assertEqual(result.timing_penalty, -6.0)

    def test_stale_penalty_capped(self) -> None:
        result = self.assessor.assess(
            is_hot_theme=True,
            has_entry_signal=True,
            bars_since_primary_event=100,
            extended_pct=0.0,
        )
        self.assertEqual(result.stage_label, StageLabel.EXTENDED_DO_NOT_CHASE)
        # 单独 stale 部分有 cap -15；总 MAX 是 -30
        self.assertEqual(result.timing_penalty, -15.0)

    def test_extended_soft_adds_penalty_without_stage_change(self) -> None:
        result = self.assessor.assess(
            is_hot_theme=True,
            has_entry_signal=True,
            bars_since_primary_event=0,
            extended_pct=18.0,
        )
        self.assertEqual(result.stage_label, StageLabel.BREAKOUT_DAY)
        self.assertEqual(result.timing_penalty, -5.0)

    def test_extended_hard_forces_do_not_chase(self) -> None:
        result = self.assessor.assess(
            is_hot_theme=True,
            has_entry_signal=True,
            bars_since_primary_event=0,
            extended_pct=30.0,
        )
        self.assertEqual(result.stage_label, StageLabel.EXTENDED_DO_NOT_CHASE)
        self.assertEqual(result.timing_penalty, -10.0)
        self.assertIn("forced_extended_from_ma", result.reasons)

    def test_stale_and_extended_combined_capped_at_max(self) -> None:
        result = self.assessor.assess(
            is_hot_theme=True,
            has_entry_signal=True,
            bars_since_primary_event=100,
            extended_pct=40.0,
        )
        self.assertEqual(result.stage_label, StageLabel.EXTENDED_DO_NOT_CHASE)
        # stale -15 + extended_hard -10 = -25，未触顶
        self.assertEqual(result.timing_penalty, -25.0)
        self.assertGreaterEqual(result.timing_penalty, self.assessor.MAX_PENALTY)

    def test_hot_with_signal_but_no_bars_remains_watch_only(self) -> None:
        result = self.assessor.assess(
            is_hot_theme=True,
            has_entry_signal=True,
            bars_since_primary_event=-1,
            extended_pct=0.0,
        )
        self.assertEqual(result.stage_label, StageLabel.WATCH_ONLY)
        self.assertEqual(result.timing_penalty, 0.0)


class AssessorFromSnapshotTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.assessor = ExtremeStrengthTimingAssessor()

    def test_from_snapshot_picks_min_non_negative_bars(self) -> None:
        snapshot = {
            "gap_breakaway": True,
            "bars_since_breakaway_gap": 7,
            "bars_since_limitup_structure_breakout": 2,
            "ma100_bars_since_breakout": 10,
            "ma100_distance_pct": 5.0,
        }
        result = self.assessor.assess_from_snapshot(snapshot, is_hot_theme=True)
        self.assertEqual(result.bars_since_primary_event, 2)
        self.assertEqual(result.stage_label, StageLabel.RETEST_ENTRY)
        self.assertEqual(result.extended_pct, 5.0)

    def test_from_snapshot_ignores_negative_bars(self) -> None:
        snapshot = {
            "is_limit_up": True,
            "bars_since_breakaway_gap": -1,
            "bars_since_limitup_structure_breakout": -1,
            "ma100_bars_since_breakout": 6,
            "ma100_distance_pct": 3.0,
        }
        result = self.assessor.assess_from_snapshot(snapshot, is_hot_theme=True)
        self.assertEqual(result.bars_since_primary_event, 6)
        self.assertEqual(result.stage_label, StageLabel.EXTENDED_DO_NOT_CHASE)

    def test_from_snapshot_returns_minus_one_when_no_event(self) -> None:
        snapshot = {
            "pattern_123_low_trendline": True,
            "ma100_distance_pct": 0.0,
        }
        result = self.assessor.assess_from_snapshot(snapshot, is_hot_theme=True)
        self.assertEqual(result.bars_since_primary_event, -1)
        self.assertEqual(result.stage_label, StageLabel.WATCH_ONLY)

    def test_from_snapshot_signed_distance_below_ma_treated_as_zero(self) -> None:
        """股价在 MA100 之下时，extended_pct 记为 0（不应因"低位"被罚)。"""
        snapshot = {
            "is_limit_up": True,
            "bars_since_limitup_structure_breakout": 0,
            "ma100_distance_pct": -8.0,
        }
        result = self.assessor.assess_from_snapshot(snapshot, is_hot_theme=True)
        self.assertEqual(result.extended_pct, 0.0)
        self.assertEqual(result.stage_label, StageLabel.BREAKOUT_DAY)
        self.assertEqual(result.timing_penalty, 0.0)

    def test_from_snapshot_none_values_tolerated(self) -> None:
        snapshot = {
            "gap_breakaway": False,
            "is_limit_up": False,
            "pattern_123_low_trendline": False,
            "bottom_divergence_double_breakout": False,
            "bars_since_breakaway_gap": None,
            "ma100_distance_pct": None,
        }
        result = self.assessor.assess_from_snapshot(snapshot, is_hot_theme=True)
        self.assertEqual(result.bars_since_primary_event, -1)
        self.assertEqual(result.extended_pct, 0.0)
        self.assertEqual(result.stage_label, StageLabel.WATCH_ONLY)


class AssessmentDataclassTestCase(unittest.TestCase):
    def test_as_dict_roundtrip(self) -> None:
        result = TimingAssessment(
            stage_label=StageLabel.BREAKOUT_DAY,
            timing_penalty=-5.0,
            bars_since_primary_event=1,
            extended_pct=17.5,
            reasons=["extended_pct>=15"],
        )
        payload = result.as_dict()
        self.assertEqual(payload["stage_label"], "breakout_day")
        self.assertEqual(payload["timing_penalty"], -5.0)
        self.assertEqual(payload["bars_since_primary_event"], 1)
        self.assertEqual(payload["extended_pct"], 17.5)
        self.assertEqual(payload["reasons"], ["extended_pct>=15"])

    def test_stage_label_is_string_enum(self) -> None:
        self.assertEqual(StageLabel.POOL_ONLY.value, "pool_only")
        self.assertEqual(StageLabel.WATCH_ONLY.value, "watch_only")
        self.assertEqual(StageLabel.BREAKOUT_DAY.value, "breakout_day")
        self.assertEqual(StageLabel.RETEST_ENTRY.value, "retest_entry")
        self.assertEqual(StageLabel.EXTENDED_DO_NOT_CHASE.value, "extended_do_not_chase")


if __name__ == "__main__":
    unittest.main()
