# -*- coding: utf-8 -*-
"""阶段 A4：CoreSignalIdentifier 的单元测试。

锁定：
- 旧接口 identify_core_signal / identify_bonus_signals 向后兼容
  且新增 signal_kind 字段
- 新接口 classify_signals 跨类别选 primary_signal：
  structure_low_entry > momentum_breakout > momentum_chase
- 跳空涨停不再自动压住低位123/底背离双突破
- SignalKind 枚举值稳定
"""

from __future__ import annotations

import unittest

from src.services.core_signal_identifier import (
    CORE_SIGNAL_SCORES,
    BONUS_SIGNAL_SCORES,
    CoreSignalIdentifier,
    SignalKind,
)


class LegacyCoreSignalCompatibilityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.identifier = CoreSignalIdentifier()

    def test_gap_limit_up_wins_inside_core(self) -> None:
        result = self.identifier.identify_core_signal(
            has_gap=True,
            has_limit_up=True,
            has_gap_breakout_ma100=True,
        )
        self.assertEqual(result["core_signal"], "跳空涨停")
        self.assertEqual(result["core_signal_score"], CORE_SIGNAL_SCORES["跳空涨停"])
        self.assertEqual(result["signal_kind"], SignalKind.MOMENTUM_CHASE.value)

    def test_gap_breakout_ma100_second(self) -> None:
        result = self.identifier.identify_core_signal(
            has_gap=False,
            has_limit_up=False,
            has_gap_breakout_ma100=True,
        )
        self.assertEqual(result["core_signal"], "缺口突破MA100")
        self.assertEqual(result["signal_kind"], SignalKind.MOMENTUM_BREAKOUT.value)

    def test_limit_up_only_third(self) -> None:
        result = self.identifier.identify_core_signal(has_limit_up=True)
        self.assertEqual(result["core_signal"], "涨停")
        self.assertEqual(result["signal_kind"], SignalKind.MOMENTUM_CHASE.value)

    def test_none_when_no_flags(self) -> None:
        result = self.identifier.identify_core_signal()
        self.assertIsNone(result["core_signal"])
        self.assertEqual(result["core_signal_score"], 0)
        self.assertEqual(result["signal_kind"], SignalKind.NONE.value)
        self.assertEqual(result["hit_reasons"], [])


class LegacyBonusSignalCompatibilityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.identifier = CoreSignalIdentifier()

    def test_low_123_only(self) -> None:
        result = self.identifier.identify_bonus_signals(has_low_123_breakout=True)
        self.assertEqual(result["bonus_signals"], ["低位123结构"])
        self.assertEqual(result["bonus_score"], BONUS_SIGNAL_SCORES["低位123结构"])
        self.assertEqual(
            result["signal_kinds"],
            [SignalKind.STRUCTURE_LOW_ENTRY.value],
        )
        self.assertEqual(result["dominant_kind"], SignalKind.STRUCTURE_LOW_ENTRY.value)

    def test_bottom_divergence_only(self) -> None:
        result = self.identifier.identify_bonus_signals(has_bottom_divergence=True)
        self.assertEqual(result["bonus_signals"], ["底背离双突破"])
        self.assertEqual(result["bonus_score"], BONUS_SIGNAL_SCORES["底背离双突破"])
        self.assertEqual(result["dominant_kind"], SignalKind.STRUCTURE_LOW_ENTRY.value)

    def test_both_bonus_signals(self) -> None:
        result = self.identifier.identify_bonus_signals(
            has_low_123_breakout=True,
            has_bottom_divergence=True,
        )
        self.assertEqual(len(result["bonus_signals"]), 2)
        self.assertEqual(
            result["bonus_score"],
            BONUS_SIGNAL_SCORES["低位123结构"] + BONUS_SIGNAL_SCORES["底背离双突破"],
        )

    def test_none_returns_empty(self) -> None:
        result = self.identifier.identify_bonus_signals()
        self.assertEqual(result["bonus_signals"], [])
        self.assertEqual(result["bonus_score"], 0)
        self.assertEqual(result["dominant_kind"], SignalKind.NONE.value)


class ClassifySignalsTestCase(unittest.TestCase):
    """新接口：跨类别按 signal_kind 选 primary_signal。"""

    def setUp(self) -> None:
        self.identifier = CoreSignalIdentifier()

    def test_no_signals_returns_none(self) -> None:
        result = self.identifier.classify_signals()
        self.assertIsNone(result["primary_signal"])
        self.assertEqual(result["primary_signal_kind"], SignalKind.NONE.value)
        self.assertEqual(result["all_signals"], [])

    def test_only_gap_limit_up_picks_gap_limit_up(self) -> None:
        result = self.identifier.classify_signals(has_gap=True, has_limit_up=True)
        self.assertEqual(result["primary_signal"], "跳空涨停")
        self.assertEqual(
            result["primary_signal_kind"],
            SignalKind.MOMENTUM_CHASE.value,
        )

    def test_only_low_123_picks_structure(self) -> None:
        result = self.identifier.classify_signals(has_low_123_breakout=True)
        self.assertEqual(result["primary_signal"], "低位123结构")
        self.assertEqual(
            result["primary_signal_kind"],
            SignalKind.STRUCTURE_LOW_ENTRY.value,
        )

    def test_structure_overrides_momentum_chase(self) -> None:
        """核心契约：低位123 + 跳空涨停共存时，primary 是低位123。"""
        result = self.identifier.classify_signals(
            has_gap=True,
            has_limit_up=True,
            has_low_123_breakout=True,
        )
        self.assertEqual(result["primary_signal"], "低位123结构")
        self.assertEqual(
            result["primary_signal_kind"],
            SignalKind.STRUCTURE_LOW_ENTRY.value,
        )
        # 兼容字段依旧保留：core_signal 仍是 跳空涨停
        self.assertEqual(result["core_signal"], "跳空涨停")
        self.assertIn("低位123结构", result["bonus_signals"])

    def test_structure_overrides_momentum_breakout(self) -> None:
        result = self.identifier.classify_signals(
            has_gap_breakout_ma100=True,
            has_bottom_divergence=True,
        )
        self.assertEqual(result["primary_signal"], "底背离双突破")
        self.assertEqual(
            result["primary_signal_kind"],
            SignalKind.STRUCTURE_LOW_ENTRY.value,
        )

    def test_momentum_breakout_beats_momentum_chase(self) -> None:
        result = self.identifier.classify_signals(
            has_limit_up=True,
            has_gap_breakout_ma100=True,
        )
        # core_signal 侧旧优先级：gap_breakout_ma100 > 涨停
        self.assertEqual(result["core_signal"], "缺口突破MA100")
        # 没有结构信号时，primary = core_signal
        self.assertEqual(result["primary_signal"], "缺口突破MA100")
        self.assertEqual(
            result["primary_signal_kind"],
            SignalKind.MOMENTUM_BREAKOUT.value,
        )

    def test_all_signals_payload_contains_kind_and_score(self) -> None:
        result = self.identifier.classify_signals(
            has_gap=True,
            has_limit_up=True,
            has_low_123_breakout=True,
            has_bottom_divergence=True,
        )
        names = {s["name"] for s in result["all_signals"]}
        self.assertEqual(names, {"跳空涨停", "低位123结构", "底背离双突破"})
        for entry in result["all_signals"]:
            self.assertIn("kind", entry)
            self.assertIn("score", entry)
            self.assertGreaterEqual(entry["score"], 0)

    def test_hit_reasons_include_both_core_and_bonus(self) -> None:
        result = self.identifier.classify_signals(
            has_gap=True,
            has_limit_up=True,
            has_low_123_breakout=True,
        )
        self.assertIn("跳空涨停（缺口+涨停共振）", result["hit_reasons"])
        self.assertIn("低位123结构+涨停突破高点2", result["hit_reasons"])


class SignalKindEnumTestCase(unittest.TestCase):
    def test_enum_values_are_stable(self) -> None:
        self.assertEqual(SignalKind.STRUCTURE_LOW_ENTRY.value, "structure_low_entry")
        self.assertEqual(SignalKind.MOMENTUM_BREAKOUT.value, "momentum_breakout")
        self.assertEqual(SignalKind.MOMENTUM_CHASE.value, "momentum_chase")
        self.assertEqual(SignalKind.NONE.value, "none")

    def test_enum_is_string_subclass(self) -> None:
        self.assertIsInstance(SignalKind.STRUCTURE_LOW_ENTRY.value, str)


if __name__ == "__main__":
    unittest.main()
