# -*- coding: utf-8 -*-
"""Tests for EntryStrategyE (Bottom Divergence Double Breakout)."""

import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from src.strategies.entry_strategies import EntryStrategyE


class TestEntryStrategyE(unittest.TestCase):
    """EntryStrategyE 底背离双突破策略测试。"""

    @staticmethod
    def _make_test_df(n: int = 150) -> pd.DataFrame:
        """构造测试用 OHLCV DataFrame。"""
        rng = np.random.RandomState(42)
        prices = np.linspace(10, 20, n) + rng.randn(n) * 0.5
        return pd.DataFrame({
            "high": prices + 0.5,
            "low": prices - 0.5,
            "close": prices,
            "volume": rng.randint(100_000, 500_000, n),
        })

    def test_entry_strategy_e_import(self):
        """EntryStrategyE 可导入。"""
        self.assertIsNotNone(EntryStrategyE)

    def test_insufficient_data(self):
        """数据不足时返回 triggered=False。"""
        df = pd.DataFrame({
            "close": [10, 11, 12],
            "high": [10.5, 11.5, 12.5],
            "low": [9.5, 10.5, 11.5],
            "volume": [100000, 100000, 100000],
        })
        result = EntryStrategyE.evaluate(df)
        self.assertFalse(result["triggered"])
        self.assertEqual(result["reason"], "insufficient data")

    def test_only_confirmed_triggers(self):
        """只有 confirmed 状态才触发。"""
        df = self._make_test_df()
        result = EntryStrategyE.evaluate(df)

        # 如果 triggered=True，则必须是 confirmed
        if result["triggered"]:
            self.assertEqual(result["pattern_123_state"], "confirmed")

    def test_entry_price_and_stop_loss_on_trigger(self):
        """触发时 entry_price 和 stop_loss_price 存在。"""
        df = self._make_test_df()
        result = EntryStrategyE.evaluate(df)

        if result["triggered"]:
            self.assertIsNotNone(result["entry_price"])
            self.assertIsNotNone(result["stop_loss_price"])
            self.assertGreater(result["entry_price"], 0)
            self.assertGreater(result["stop_loss_price"], 0)

    @patch("src.strategies.entry_strategies.BottomDivergenceBreakoutDetector.detect")
    def test_extended_confirmed_signal_does_not_trigger(self, detect_mock):
        """历史已确认但偏离确认价过远时不触发直接策略入口。"""
        df = self._make_test_df()
        detect_mock.return_value = {
            "state": "confirmed",
            "pattern_code": "price_down_macd_up",
            "pattern_label": "经典底背离",
            "entry_price": 10.0,
            "stop_loss_price": 9.0,
            "signal_strength": 0.8,
            "confirmation_bar_index": len(df) - 2,
        }

        result = EntryStrategyE.evaluate(df)

        self.assertFalse(result["triggered"])
        self.assertFalse(result["actionable_entry"])
        self.assertIn("not in actionable entry window", result["reason"])

    @patch("src.strategies.entry_strategies.BottomDivergenceBreakoutDetector.detect")
    def test_weak_pattern_confirmed_signal_does_not_trigger(self, detect_mock):
        """强势回撤类 confirmed 不触发底背离买点策略。"""
        df = self._make_test_df()
        latest_close = float(df.iloc[-1]["close"])
        detect_mock.return_value = {
            "state": "confirmed",
            "pattern_code": "price_up_macd_flat",
            "pattern_label": "强势回撤·MACD持平",
            "entry_price": latest_close,
            "stop_loss_price": latest_close * 0.9,
            "signal_strength": 0.8,
            "confirmation_bar_index": len(df) - 1,
        }

        result = EntryStrategyE.evaluate(df)

        self.assertFalse(result["triggered"])
        self.assertFalse(result["actionable_entry"])

    def test_result_schema(self):
        """结果 schema 完整。"""
        df = self._make_test_df()
        result = EntryStrategyE.evaluate(df)

        required_keys = [
            "triggered",
            "pattern_123",
            "pattern_123_state",
            "entry_price",
            "stop_loss_price",
            "score",
            "reason",
        ]
        for key in required_keys:
            self.assertIn(key, result, f"Missing key: {key}")

        self.assertIsInstance(result["triggered"], bool)
        self.assertIsInstance(result["score"], (int, float))
        self.assertGreaterEqual(result["score"], 0)
        self.assertLessEqual(result["score"], 100)


if __name__ == "__main__":
    unittest.main()
