# -*- coding: utf-8 -*-
"""
Tests for FactorService integration with BottomDivergenceBreakoutDetector.
"""

import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from src.services.factor_service import FactorService


class TestFactorServiceBottomDivergence(unittest.TestCase):
    """FactorService 与底背离检测器的集成测试。"""

    @staticmethod
    def _make_test_df(n: int = 150) -> pd.DataFrame:
        """构造测试用 OHLCV DataFrame。"""
        rng = np.random.RandomState(42)
        prices = np.linspace(10, 20, n) + rng.randn(n) * 0.5
        return pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=n),
            "code": "TEST001",
            "open": prices,
            "high": prices + 0.5,
            "low": prices - 0.5,
            "close": prices,
            "volume": rng.randint(100_000, 500_000, n),
            "amount": prices * rng.randint(100_000, 500_000, n),
            "pct_chg": rng.randn(n) * 2,
        })

    def test_factor_service_has_bottom_divergence_factors(self):
        """FactorService 输出包含底背离因子。"""
        df = self._make_test_df()
        service = FactorService()

        # 调用 _compute_extended_factors（内部方法，用于测试）
        result = service._compute_extended_factors(df, df.iloc[-1], df["close"])

        # 检查所有底背离因子存在
        expected_keys = [
            "bottom_divergence_double_breakout",
            "bottom_divergence_state",
            "bottom_divergence_pattern_code",
            "bottom_divergence_pattern_label",
            "bottom_divergence_signal_strength",
            "bottom_divergence_entry_price",
            "bottom_divergence_stop_loss",
            "bottom_divergence_horizontal_breakout",
            "bottom_divergence_trendline_breakout",
            "bottom_divergence_sync_breakout",
            "bottom_divergence_actionable_entry",
            "bottom_divergence_confirmation_days",
        ]
        for key in expected_keys:
            self.assertIn(key, result, f"Missing factor: {key}")

    def test_bottom_divergence_factors_types(self):
        """底背离因子类型正确。"""
        df = self._make_test_df()
        service = FactorService()
        result = service._compute_extended_factors(df, df.iloc[-1], df["close"])

        # 布尔因子
        self.assertIsInstance(result["bottom_divergence_double_breakout"], bool)
        self.assertIsInstance(result["bottom_divergence_horizontal_breakout"], bool)
        self.assertIsInstance(result["bottom_divergence_trendline_breakout"], bool)
        self.assertIsInstance(result["bottom_divergence_sync_breakout"], bool)

        # 字符串因子
        self.assertIsInstance(result["bottom_divergence_state"], str)
        self.assertIn(
            result["bottom_divergence_state"],
            ("rejected", "divergence_only", "structure_ready", "confirmed", "late_or_weak"),
        )

        # 数值因子
        self.assertIsInstance(result["bottom_divergence_signal_strength"], (int, float))
        self.assertGreaterEqual(result["bottom_divergence_signal_strength"], 0.0)
        self.assertLessEqual(result["bottom_divergence_signal_strength"], 1.0)

    def test_insufficient_data_safe_degradation(self):
        """数据不足时安全降级。"""
        df = self._make_test_df(n=20)  # 太少
        service = FactorService()
        result = service._compute_extended_factors(df, df.iloc[-1], df["close"])

        # 所有因子应该有默认值
        self.assertFalse(result["bottom_divergence_double_breakout"])
        self.assertEqual(result["bottom_divergence_state"], "rejected")
        self.assertEqual(result["bottom_divergence_signal_strength"], 0.0)
        self.assertIsNone(result["bottom_divergence_entry_price"])
        self.assertIsNone(result["bottom_divergence_stop_loss"])

    def test_confirmed_state_matches_boolean_factor(self):
        """confirmed 状态与布尔因子一致。"""
        df = self._make_test_df()
        service = FactorService()
        result = service._compute_extended_factors(df, df.iloc[-1], df["close"])

        # 如果 state == "confirmed"，则 double_breakout 应为 True
        if result["bottom_divergence_state"] == "confirmed":
            self.assertTrue(result["bottom_divergence_double_breakout"])
        # 反之亦然
        if result["bottom_divergence_double_breakout"]:
            self.assertEqual(result["bottom_divergence_state"], "confirmed")

    def test_pattern_code_transparency(self):
        """pattern_code 正确透传。"""
        df = self._make_test_df()
        service = FactorService()
        result = service._compute_extended_factors(df, df.iloc[-1], df["close"])

        # pattern_code 可以是 None 或有效的六种形态之一
        valid_codes = {
            "price_down_macd_up",
            "price_down_macd_flat",
            "price_flat_macd_up",
            "price_flat_macd_down",
            "price_up_macd_down",
            "price_up_macd_flat",
        }
        if result["bottom_divergence_pattern_code"] is not None:
            self.assertIn(result["bottom_divergence_pattern_code"], valid_codes)

    def test_hit_reasons_factor_exists(self):
        """FactorService 输出包含 bottom_divergence_hit_reasons 因子。"""
        df = self._make_test_df()
        service = FactorService()
        result = service._compute_extended_factors(df, df.iloc[-1], df["close"])
        self.assertIn("bottom_divergence_hit_reasons", result)
        self.assertIsInstance(result["bottom_divergence_hit_reasons"], list)

    def test_hit_reasons_empty_for_short_data(self):
        """数据不足时 hit_reasons 为空列表。"""
        df = self._make_test_df(n=20)
        service = FactorService()
        result = service._compute_extended_factors(df, df.iloc[-1], df["close"])
        self.assertEqual(result["bottom_divergence_hit_reasons"], [])

    @patch("src.services.factor_service.BottomDivergenceBreakoutDetector.detect")
    def test_confirmation_days_tracks_latest_double_breakout_bar(self, detect_mock):
        df = self._make_test_df(n=80)
        detect_mock.return_value = {
            "state": "confirmed",
            "pattern_code": "price_flat_macd_up",
            "pattern_label": "价格持平·MACD抬升",
            "signal_strength": 0.82,
            "entry_price": 12.3,
            "stop_loss_price": 10.8,
            "horizontal_breakout_confirmed": True,
            "trendline_breakout_confirmed": True,
            "double_breakout_sync": True,
            "downtrend_line": {"breakout_bar_index": 76},
            "hit_reasons": ["mocked"],
        }

        service = FactorService()
        result = service._compute_extended_factors(df, df.iloc[-1], df["close"])

        self.assertIn("bottom_divergence_confirmation_days", result)
        self.assertEqual(result["bottom_divergence_confirmation_days"], 3)

    @patch("src.services.factor_service.BottomDivergenceBreakoutDetector.detect")
    def test_confirmed_bottom_divergence_fields_are_preserved(self, detect_mock):
        df = self._make_test_df(n=90)
        latest_close = float(df.iloc[-1]["close"])
        detect_mock.return_value = {
            "state": "confirmed",
            "pattern_code": "price_down_macd_up",
            "pattern_label": "经典底背离",
            "signal_strength": 0.91,
            "entry_price": latest_close,
            "stop_loss_price": 10.6,
            "horizontal_breakout_confirmed": True,
            "trendline_breakout_confirmed": True,
            "double_breakout_sync": True,
            "confirmation_bar_index": 89,
            "hit_reasons": ["底背离成立", "双突破同步确认"],
        }

        result = FactorService._compute_bottom_divergence_factors(df)

        self.assertTrue(result["bottom_divergence_double_breakout"])
        self.assertEqual(result["bottom_divergence_state"], "confirmed")
        self.assertEqual(result["bottom_divergence_pattern_code"], "price_down_macd_up")
        self.assertEqual(result["bottom_divergence_pattern_label"], "经典底背离")
        self.assertAlmostEqual(result["bottom_divergence_signal_strength"], 0.91)
        self.assertEqual(result["bottom_divergence_entry_price"], latest_close)
        self.assertEqual(result["bottom_divergence_stop_loss"], 10.6)
        self.assertTrue(result["bottom_divergence_horizontal_breakout"])
        self.assertTrue(result["bottom_divergence_trendline_breakout"])
        self.assertTrue(result["bottom_divergence_sync_breakout"])
        self.assertTrue(result["bottom_divergence_actionable_entry"])
        self.assertEqual(result["bottom_divergence_confirmation_days"], 0)
        self.assertEqual(result["bottom_divergence_hit_reasons"], ["底背离成立", "双突破同步确认"])
        self.assertEqual(result["bottom_divergence_entry_zone"], "just_double_breakout")
        self.assertEqual(result["bottom_divergence_entry_timing_score"], 1.0)

    @patch("src.services.factor_service.BottomDivergenceBreakoutDetector.detect")
    def test_confirmed_bottom_divergence_fields_reach_extended_factors(self, detect_mock):
        df = self._make_test_df(n=90)
        latest_close = float(df.iloc[-1]["close"])
        detect_mock.return_value = {
            "state": "confirmed",
            "pattern_code": "price_down_macd_up",
            "pattern_label": "经典底背离",
            "signal_strength": 0.91,
            "entry_price": latest_close,
            "stop_loss_price": 10.6,
            "horizontal_breakout_confirmed": True,
            "trendline_breakout_confirmed": True,
            "double_breakout_sync": True,
            "confirmation_bar_index": 89,
            "hit_reasons": ["底背离成立", "双突破同步确认"],
        }

        service = FactorService()
        result = service._compute_extended_factors(df, df.iloc[-1], df["close"])

        self.assertTrue(result["bottom_divergence_double_breakout"])
        self.assertEqual(result["bottom_divergence_state"], "confirmed")
        self.assertEqual(result["bottom_divergence_pattern_code"], "price_down_macd_up")
        self.assertEqual(result["bottom_divergence_pattern_label"], "经典底背离")
        self.assertAlmostEqual(result["bottom_divergence_signal_strength"], 0.91)
        self.assertEqual(result["bottom_divergence_entry_price"], latest_close)
        self.assertEqual(result["bottom_divergence_stop_loss"], 10.6)
        self.assertTrue(result["bottom_divergence_horizontal_breakout"])
        self.assertTrue(result["bottom_divergence_trendline_breakout"])
        self.assertTrue(result["bottom_divergence_sync_breakout"])
        self.assertTrue(result["bottom_divergence_actionable_entry"])
        self.assertEqual(result["bottom_divergence_confirmation_days"], 0)
        self.assertEqual(result["bottom_divergence_hit_reasons"], ["底背离成立", "双突破同步确认"])
        self.assertEqual(result["bottom_divergence_entry_zone"], "just_double_breakout")

    @patch("src.services.factor_service.BottomDivergenceBreakoutDetector.detect")
    def test_extended_confirmed_bottom_divergence_is_not_main_breakout(self, detect_mock):
        """Confirmed historical divergence should be suppressed once price is extended."""
        df = self._make_test_df(n=90)
        detect_mock.return_value = {
            "state": "confirmed",
            "pattern_code": "price_down_macd_up",
            "pattern_label": "经典底背离",
            "signal_strength": 0.91,
            "entry_price": 10.0,
            "stop_loss_price": 8.8,
            "horizontal_breakout_confirmed": True,
            "trendline_breakout_confirmed": True,
            "double_breakout_sync": True,
            "confirmation_bar_index": 88,
            "hit_reasons": ["底背离成立", "双突破同步确认"],
        }

        result = FactorService._compute_bottom_divergence_factors(df)

        self.assertTrue(result["bottom_divergence_double_breakout"])
        self.assertFalse(result["bottom_divergence_actionable_entry"])
        self.assertEqual(result["bottom_divergence_state"], "confirmed")
        self.assertEqual(result["bottom_divergence_validation_status"], "extended_not_entry")
        self.assertEqual(result["bottom_divergence_entry_zone"], "extended_not_entry")
        self.assertGreater(result["bottom_divergence_extended_pct"], 10.0)
        self.assertEqual(result["bottom_divergence_entry_timing_score"], 0.0)

    @patch("src.services.factor_service.BottomDivergenceBreakoutDetector.detect")
    def test_failed_breakout_below_entry_is_not_actionable(self, detect_mock):
        """A post-confirmation drop below entry price is not a valid near-entry setup."""
        df = self._make_test_df(n=90)
        latest_close = float(df.iloc[-1]["close"])
        detect_mock.return_value = {
            "state": "confirmed",
            "pattern_code": "price_down_macd_up",
            "pattern_label": "经典底背离",
            "signal_strength": 0.91,
            "entry_price": latest_close * 1.2,
            "stop_loss_price": latest_close * 0.9,
            "horizontal_breakout_confirmed": True,
            "trendline_breakout_confirmed": True,
            "double_breakout_sync": True,
            "confirmation_bar_index": 88,
            "hit_reasons": ["底背离成立", "双突破同步确认"],
        }

        result = FactorService._compute_bottom_divergence_factors(df)

        self.assertTrue(result["bottom_divergence_double_breakout"])
        self.assertFalse(result["bottom_divergence_actionable_entry"])
        self.assertEqual(result["bottom_divergence_validation_status"], "extended_not_entry")

    @patch("src.services.factor_service.BottomDivergenceBreakoutDetector.detect")
    def test_weak_bottom_divergence_pattern_is_not_actionable_entry(self, detect_mock):
        """Weak/continuation patterns can be transparent but should not enter main screening."""
        df = self._make_test_df(n=90)
        latest_close = float(df.iloc[-1]["close"])
        detect_mock.return_value = {
            "state": "confirmed",
            "pattern_code": "price_up_macd_flat",
            "pattern_label": "强势回撤·MACD持平",
            "signal_strength": 0.82,
            "entry_price": latest_close,
            "stop_loss_price": latest_close * 0.9,
            "horizontal_breakout_confirmed": True,
            "trendline_breakout_confirmed": True,
            "double_breakout_sync": True,
            "confirmation_bar_index": 89,
            "hit_reasons": ["强势回撤", "双突破同步确认"],
        }

        result = FactorService._compute_bottom_divergence_factors(df)

        self.assertTrue(result["bottom_divergence_double_breakout"])
        self.assertFalse(result["bottom_divergence_actionable_entry"])
        self.assertFalse(result["bottom_divergence_valid_pattern"])


if __name__ == "__main__":
    unittest.main()
