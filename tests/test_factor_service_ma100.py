# -*- coding: utf-8 -*-
"""
TDD tests for Phase 1f: FactorService MA100 factor extension.

Tests verify that _compute_extended_factors and helper methods produce
the new MA100 and gap/limit-up factors correctly.
"""

import unittest
import numpy as np
import pandas as pd

from src.services.factor_service import FactorService


def _make_group(n: int = 120, base: float = 10.0, trend: float = 0.002) -> pd.DataFrame:
    """Generate a stock group DataFrame mimicking DB rows."""
    np.random.seed(42)
    prices = [base]
    for _ in range(n - 1):
        change = np.random.randn() * 0.01 + trend
        prices.append(prices[-1] * (1 + change))
    close = np.array(prices)
    dates = pd.date_range(start="2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "date": dates,
        "open": close * 0.999,
        "high": close * (1 + np.random.uniform(0, 0.015, n)),
        "low": close * (1 - np.random.uniform(0, 0.015, n)),
        "close": close,
        "volume": np.random.randint(1_000_000, 5_000_000, n),
        "pct_chg": np.concatenate([[0], np.diff(close) / close[:-1] * 100]),
    })


class TestComputeMA100Factors(unittest.TestCase):

    def test_returns_ma100_fields(self):
        group = _make_group(n=120)
        close_series = group["close"].astype(float)
        result = FactorService._compute_ma100_factors(group, close_series, float(close_series.iloc[-1]))
        self.assertIn("ma100", result)
        self.assertIn("above_ma100", result)
        self.assertIn("ma100_distance_pct", result)
        self.assertIn("ma100_breakout_days", result)
        self.assertIn("pullback_ma100", result)
        self.assertIn("pullback_ma20", result)
        self.assertIn("stop_loss_price", result)
        self.assertIn("stop_loss_ma", result)

    def test_ma100_positive_in_uptrend(self):
        group = _make_group(n=120, trend=0.003)
        close_series = group["close"].astype(float)
        result = FactorService._compute_ma100_factors(group, close_series, float(close_series.iloc[-1]))
        self.assertGreater(result["ma100"], 0)
        self.assertTrue(result["above_ma100"])

    def test_ma100_zero_when_insufficient(self):
        group = _make_group(n=50)
        close_series = group["close"].astype(float)
        result = FactorService._compute_ma100_factors(group, close_series, float(close_series.iloc[-1]))
        self.assertEqual(result["ma100"], 0.0)


class TestComputeGapLimitFactors(unittest.TestCase):

    def test_returns_gap_limit_fields(self):
        group = _make_group(n=60)
        result = FactorService._compute_gap_limit_factors(group)
        self.assertIn("gap_up", result)
        self.assertIn("gap_breakaway", result)
        self.assertIn("is_limit_up", result)
        self.assertIn("limit_up_breakout", result)

    def test_no_gap_in_normal_data(self):
        group = _make_group(n=60)
        result = FactorService._compute_gap_limit_factors(group)
        self.assertFalse(result["gap_breakaway"])


class TestExtendedFactorsIntegration(unittest.TestCase):

    def test_extended_factors_include_ma100(self):
        fs = FactorService.__new__(FactorService)
        group = _make_group(n=120)
        latest = group.iloc[-1]
        close_series = group["close"].astype(float)
        result = fs._compute_extended_factors(group, latest, close_series)
        self.assertIn("ma100", result)
        self.assertIn("above_ma100", result)
        self.assertIn("gap_up", result)
        self.assertIn("is_limit_up", result)
        # Original factors still present
        self.assertIn("pct_chg_5d", result)
        self.assertIn("candle_pattern", result)

    def test_extended_factors_include_ma100_60min(self):
        fs = FactorService.__new__(FactorService)
        group = _make_group(n=120)
        latest = group.iloc[-1]
        close_series = group["close"].astype(float)
        result = fs._compute_extended_factors(group, latest, close_series)
        self.assertIn("ma100_60min_confirmed", result)
        self.assertIn("ma100_60min_freshness_score", result)
        self.assertIn("ma100_60min_ma_score", result)
        self.assertIn("ma100_60min_hit_reasons", result)


class TestMA10060minCombinedFactors(unittest.TestCase):
    """Gate semantics use the new real-crossing fields:
    ``ma100_bars_since_breakout`` + pre-breakout context + distance pct.
    """

    @staticmethod
    def _fresh_factors(**overrides) -> dict:
        """Default snapshot representing a textbook fresh MA100 breakout."""
        base = {
            "above_ma100": True,
            "ma100_breakout_days": 3,
            "ma100_bars_since_breakout": 2,
            "ma100_breakout_bar_index": 97,
            "ma100_pre_breakout_below_ratio": 0.8,
            "ma100_pre_breakout_consecutive_below_bars": 5,
            "ma100": 50.0,
            "ma100_distance_pct": 2.0,
        }
        base.update(overrides)
        return base

    def test_confirmed_when_fresh_real_breakout(self):
        result = FactorService._compute_ma100_60min_combined_factors(self._fresh_factors())
        self.assertTrue(result["ma100_60min_confirmed"])
        self.assertGreater(result["ma100_60min_freshness_score"], 0)
        self.assertGreater(result["ma100_60min_ma_score"], 0)

    def test_rejected_when_stale_breakout(self):
        result = FactorService._compute_ma100_60min_combined_factors(
            self._fresh_factors(ma100_bars_since_breakout=8)
        )
        self.assertFalse(result["ma100_60min_confirmed"])
        self.assertEqual(result["ma100_60min_freshness_score"], 0.0)

    def test_rejected_when_below_ma100(self):
        result = FactorService._compute_ma100_60min_combined_factors(
            self._fresh_factors(above_ma100=False, ma100_distance_pct=-3.0)
        )
        self.assertFalse(result["ma100_60min_confirmed"])

    def test_rejected_when_no_real_crossing_found(self):
        """bars_since_breakout=-1 means the detector found no real upward
        crossing — even if the stock is nominally above MA100, we must not
        label it a fresh breakout."""
        result = FactorService._compute_ma100_60min_combined_factors(
            self._fresh_factors(ma100_bars_since_breakout=-1)
        )
        self.assertFalse(result["ma100_60min_confirmed"])

    def test_rejected_when_price_too_far_from_ma100(self):
        """Hard distance gate: already left the best-buy zone → reject."""
        result = FactorService._compute_ma100_60min_combined_factors(
            self._fresh_factors(ma100_distance_pct=9.5)
        )
        self.assertFalse(result["ma100_60min_confirmed"])

    def test_rejected_when_pre_breakout_background_missing(self):
        """Without enough pre-breakout 'below MA100' context, the crossing
        is more likely a noise flip of an already-elevated name — reject."""
        result = FactorService._compute_ma100_60min_combined_factors(
            self._fresh_factors(
                ma100_pre_breakout_below_ratio=0.2,
                ma100_pre_breakout_consecutive_below_bars=1,
            )
        )
        self.assertFalse(result["ma100_60min_confirmed"])

    def test_confirmed_when_consecutive_below_satisfied_even_with_low_ratio(self):
        """Either ratio OR consecutive-below signal should suffice."""
        result = FactorService._compute_ma100_60min_combined_factors(
            self._fresh_factors(
                ma100_pre_breakout_below_ratio=0.3,
                ma100_pre_breakout_consecutive_below_bars=4,
            )
        )
        self.assertTrue(result["ma100_60min_confirmed"])

    def test_hit_reasons_include_60min_guidance(self):
        result = FactorService._compute_ma100_60min_combined_factors(
            self._fresh_factors(
                ma100_bars_since_breakout=1,
                ma100=48.50,
                ma100_distance_pct=3.1,
            )
        )
        reasons = result["ma100_60min_hit_reasons"]
        self.assertEqual(len(reasons), 2)
        self.assertIn("MA100站稳确认", reasons[0])
        self.assertIn("60分钟入场提示", reasons[1])
        self.assertIn("48.50", reasons[1])

    def test_freshness_score_decreases_with_bars_since(self):
        scores = []
        for bars in [0, 2, 4]:
            result = FactorService._compute_ma100_60min_combined_factors(
                self._fresh_factors(ma100_bars_since_breakout=bars)
            )
            scores.append(result["ma100_60min_freshness_score"])
        self.assertGreater(scores[0], scores[1])
        self.assertGreater(scores[1], scores[2])
        # b=0 → 1.0, b=2 → 0.8, b=4 → 0.6
        self.assertAlmostEqual(scores[0], 1.0)
        self.assertAlmostEqual(scores[1], 0.8)
        self.assertAlmostEqual(scores[2], 0.6)


class TestMA10060minCombinedScenarios(unittest.TestCase):
    """四类真实场景（首次突破 / 回踩收复 / 长期上方 / 远离均线）的行为区分。"""

    def test_first_real_breakout_after_long_downside(self):
        """长期在 MA100 下方后今日首次上穿 → 入选。"""
        factors = {
            "above_ma100": True,
            "ma100_breakout_days": 1,
            "ma100_bars_since_breakout": 0,
            "ma100_breakout_bar_index": 99,
            "ma100_pre_breakout_below_ratio": 0.95,
            "ma100_pre_breakout_consecutive_below_bars": 18,
            "ma100": 20.0,
            "ma100_distance_pct": 1.2,
        }
        result = FactorService._compute_ma100_60min_combined_factors(factors)
        self.assertTrue(result["ma100_60min_confirmed"])
        self.assertAlmostEqual(result["ma100_60min_freshness_score"], 1.0)

    def test_recently_recovered_after_brief_dip_is_a_fresh_breakout(self):
        """长期在上方后短暂跌破再收回，若检测到真实上穿、前置条件满足则入选。"""
        factors = {
            "above_ma100": True,
            "ma100_breakout_days": 2,
            "ma100_bars_since_breakout": 1,
            "ma100_breakout_bar_index": 98,
            "ma100_pre_breakout_below_ratio": 0.65,
            "ma100_pre_breakout_consecutive_below_bars": 4,
            "ma100": 30.0,
            "ma100_distance_pct": 1.8,
        }
        result = FactorService._compute_ma100_60min_combined_factors(factors)
        self.assertTrue(result["ma100_60min_confirmed"])

    def test_long_above_ma100_without_real_crossing_is_rejected(self):
        """连续站上 MA100 已久，未发现近 5 根内的真实上穿 → 不入选（原语义偏差场景）。"""
        factors = {
            "above_ma100": True,
            "ma100_breakout_days": 40,
            "ma100_bars_since_breakout": 35,
            "ma100_breakout_bar_index": 64,
            "ma100_pre_breakout_below_ratio": 0.9,
            "ma100_pre_breakout_consecutive_below_bars": 10,
            "ma100": 30.0,
            "ma100_distance_pct": 1.5,
        }
        result = FactorService._compute_ma100_60min_combined_factors(factors)
        self.assertFalse(result["ma100_60min_confirmed"])

    def test_far_from_ma100_is_rejected_even_with_fresh_crossing(self):
        """虽然真实上穿发生在近 5 根内，但价格已明显远离 MA100 → 不入选。"""
        factors = {
            "above_ma100": True,
            "ma100_breakout_days": 3,
            "ma100_bars_since_breakout": 2,
            "ma100_breakout_bar_index": 97,
            "ma100_pre_breakout_below_ratio": 0.9,
            "ma100_pre_breakout_consecutive_below_bars": 6,
            "ma100": 30.0,
            "ma100_distance_pct": 12.0,
        }
        result = FactorService._compute_ma100_60min_combined_factors(factors)
        self.assertFalse(result["ma100_60min_confirmed"])


class TestMA100Low123CombinedFactors(unittest.TestCase):

    def _make_raw_group(self) -> pd.DataFrame:
        dates = pd.date_range(start="2025-02-01", periods=80, freq="D")
        close = np.linspace(80, 100, 80)
        return pd.DataFrame({
            "date": dates,
            "open": close * 0.995,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": np.full(80, 1_000_000.0),
        })

    def test_confirmed_when_above_ma100_and_low123_breakout_ready(self):
        group = self._make_raw_group()
        ma100_factors = {
            "above_ma100": True,
            "ma100_breakout_days": 2,
            "ma100": 95.0,
            "ma100_distance_pct": 1.8,
        }
        pattern_123_factors = {
            "pattern_123_low_trendline": True,
            "pattern_123_breakout_ready": True,
            "pattern_123_watchlist": False,
            "pattern_123_state": "breakout_ready",
            "pattern_123_entry_price": 98.5,
            "pattern_123_stop_loss": 92.0,
            "pattern_123_signal_strength": 0.84,
        }
        pattern_123_raw = {
            "point1": {"idx": 60, "price": 88.0},
            "point2": {"idx": 67, "price": 96.0},
            "point3": {"idx": 72, "price": 91.0},
            "breakout_p2_bar_index": 77,
            "downtrend_line": {
                "found": True,
                "touch_count": 3,
                "slope": -0.12,
                "touch_points": [{"idx": 55, "price": 102.0}, {"idx": 61, "price": 99.0}],
                "breakout_bar_index": 77,
                "projected_value_at_breakout": 97.4,
                "breakout_confirmed": True,
            },
            "breakout_point2_confirmed": True,
            "breakout_trendline_confirmed": True,
        }

        result = FactorService._compute_ma100_low123_combined_factors(
            ma100_factors,
            pattern_123_factors,
            pattern_123_raw,
            group,
        )

        self.assertTrue(result["ma100_low123_confirmed"])
        self.assertFalse(result["ma100_low123_watchlist"])
        self.assertTrue(result["ma100_low123_data_complete"])
        self.assertAlmostEqual(result["ma100_low123_pattern_strength"], 0.84)
        self.assertGreater(result["ma100_low123_ma_score"], 0.0)
        self.assertEqual(result["ma100_low123_validation_status"], "confirmed")
        self.assertIsNone(result["ma100_low123_validation_reason"])
        self.assertGreaterEqual(len(result["ma100_low123_hit_reasons"]), 4)
        self.assertIn("123结构", "".join(result["ma100_low123_hit_reasons"]))
        self.assertIn("同步突破", "".join(result["ma100_low123_hit_reasons"]))
        self.assertIn("MA100站上确认", "".join(result["ma100_low123_hit_reasons"]))

    def test_rejected_when_low123_breakout_is_stale(self):
        """Hard freshness gate: stale Low123 breakout should reject MA100 combo."""
        group = self._make_raw_group()
        ma100_factors = {
            "above_ma100": True,
            "ma100_breakout_days": 2,
            "ma100": 95.0,
            "ma100_distance_pct": 1.8,
        }
        pattern_123_factors = {
            "pattern_123_low_trendline": True,
            "pattern_123_breakout_ready": True,
            "pattern_123_watchlist": False,
            "pattern_123_state": "breakout_ready",
            "pattern_123_entry_price": 98.5,
            "pattern_123_stop_loss": 92.0,
            "pattern_123_signal_strength": 0.84,
        }
        pattern_123_raw = {
            "point1": {"idx": 60, "price": 88.0},
            "point2": {"idx": 67, "price": 96.0},
            "point3": {"idx": 72, "price": 91.0},
            "breakout_p2_bar_index": 73,
            "downtrend_line": {
                "found": True,
                "touch_count": 3,
                "slope": -0.12,
                "touch_points": [{"idx": 55, "price": 102.0}, {"idx": 61, "price": 99.0}],
                "breakout_bar_index": 73,
                "projected_value_at_breakout": 95.4,
                "breakout_confirmed": True,
            },
            "breakout_point2_confirmed": True,
            "breakout_trendline_confirmed": True,
        }

        result = FactorService._compute_ma100_low123_combined_factors(
            ma100_factors,
            pattern_123_factors,
            pattern_123_raw,
            group,
        )

        self.assertFalse(result["ma100_low123_confirmed"])
        self.assertTrue(result["ma100_low123_data_complete"])
        self.assertEqual(result["ma100_low123_pattern_strength"], 0.0)
        self.assertEqual(result["ma100_low123_ma_score"], 0.0)
        self.assertEqual(result["ma100_low123_validation_status"], "stale_breakout")
        self.assertEqual(result["ma100_low123_validation_reason"], "stale_breakout")
        self.assertEqual(result["ma100_low123_hit_reasons"], [])

    def test_allows_breakout_exactly_three_bars_old(self):
        group = self._make_raw_group()
        ma100_factors = {
            "above_ma100": True,
            "ma100_breakout_days": 2,
            "ma100": 95.0,
            "ma100_distance_pct": 1.8,
        }
        pattern_123_factors = {
            "pattern_123_low_trendline": True,
            "pattern_123_breakout_ready": True,
            "pattern_123_watchlist": False,
            "pattern_123_state": "breakout_ready",
            "pattern_123_entry_price": 98.5,
            "pattern_123_stop_loss": 92.0,
            "pattern_123_signal_strength": 0.84,
        }
        pattern_123_raw = {
            "breakout_p2_bar_index": 76,
        }

        result = FactorService._compute_ma100_low123_combined_factors(
            ma100_factors,
            pattern_123_factors,
            pattern_123_raw,
            group,
        )

        self.assertTrue(result["ma100_low123_confirmed"])

    def test_rejects_breakout_four_bars_old(self):
        group = self._make_raw_group()
        ma100_factors = {
            "above_ma100": True,
            "ma100_breakout_days": 2,
            "ma100": 95.0,
            "ma100_distance_pct": 1.8,
        }
        pattern_123_factors = {
            "pattern_123_low_trendline": True,
            "pattern_123_breakout_ready": True,
            "pattern_123_watchlist": False,
            "pattern_123_state": "breakout_ready",
            "pattern_123_entry_price": 98.5,
            "pattern_123_stop_loss": 92.0,
            "pattern_123_signal_strength": 0.84,
        }
        pattern_123_raw = {
            "breakout_p2_bar_index": 75,
        }

        result = FactorService._compute_ma100_low123_combined_factors(
            ma100_factors,
            pattern_123_factors,
            pattern_123_raw,
            group,
        )

        self.assertFalse(result["ma100_low123_confirmed"])
        self.assertEqual(result["ma100_low123_validation_status"], "stale_breakout")
        self.assertEqual(result["ma100_low123_validation_reason"], "stale_breakout")

    def test_watching_state_stays_in_watchlist_not_confirmed(self):
        group = self._make_raw_group()
        ma100_factors = {
            "above_ma100": True,
            "ma100_breakout_days": 2,
            "ma100": 95.0,
            "ma100_distance_pct": 1.8,
        }
        pattern_123_factors = {
            "pattern_123_low_trendline": False,
            "pattern_123_breakout_ready": False,
            "pattern_123_watchlist": True,
            "pattern_123_state": "watching",
            "pattern_123_entry_price": None,
            "pattern_123_stop_loss": None,
            "pattern_123_signal_strength": 0.41,
        }

        result = FactorService._compute_ma100_low123_combined_factors(
            ma100_factors,
            pattern_123_factors,
            {},
            group,
        )

        self.assertFalse(result["ma100_low123_confirmed"])
        self.assertTrue(result["ma100_low123_watchlist"])
        self.assertFalse(result["ma100_low123_data_complete"])
        self.assertGreater(result["ma100_low123_pattern_strength"], 0.0)
        self.assertGreater(result["ma100_low123_ma_score"], 0.0)
        self.assertEqual(result["ma100_low123_validation_status"], "watching")
        self.assertEqual(result["ma100_low123_validation_reason"], "watching")
        self.assertEqual(result["ma100_low123_hit_reasons"], [])
        self.assertGreaterEqual(len(result["ma100_low123_watch_hit_reasons"]), 1)

    def test_breakout_ready_state_can_confirm_even_without_legacy_bool(self):
        """Combined gate should follow the new breakout_ready semantics."""
        group = self._make_raw_group()
        ma100_factors = {
            "above_ma100": True,
            "ma100_breakout_days": 2,
            "ma100": 95.0,
            "ma100_distance_pct": 1.8,
        }
        pattern_123_factors = {
            "pattern_123_low_trendline": False,
            "pattern_123_breakout_ready": True,
            "pattern_123_watchlist": False,
            "pattern_123_state": "breakout_ready",
            "pattern_123_entry_price": 98.5,
            "pattern_123_stop_loss": 92.0,
            "pattern_123_signal_strength": 0.84,
        }
        pattern_123_raw = {
            "breakout_p2_bar_index": 77,
            "point1": {"idx": 60, "price": 88.0},
            "point2": {"idx": 67, "price": 96.0},
            "point3": {"idx": 72, "price": 91.0},
        }

        result = FactorService._compute_ma100_low123_combined_factors(
            ma100_factors,
            pattern_123_factors,
            pattern_123_raw,
            group,
        )

        self.assertTrue(result["ma100_low123_confirmed"])
        self.assertEqual(result["ma100_low123_validation_status"], "confirmed")
        self.assertIsNone(result["ma100_low123_validation_reason"])

    def test_missing_breakout_bar_index_is_tagged_for_shadow_monitoring(self):
        """Missing breakout index stays observable before fail-closed rollout."""
        group = self._make_raw_group()
        ma100_factors = {
            "above_ma100": True,
            "ma100_breakout_days": 2,
            "ma100": 95.0,
            "ma100_distance_pct": 1.8,
        }
        pattern_123_factors = {
            "pattern_123_low_trendline": True,
            "pattern_123_breakout_ready": True,
            "pattern_123_watchlist": False,
            "pattern_123_state": "breakout_ready",
            "pattern_123_entry_price": 98.5,
            "pattern_123_stop_loss": 92.0,
            "pattern_123_signal_strength": 0.84,
        }
        pattern_123_raw = {
            "downtrend_line": {
                "found": True,
                "touch_count": 3,
                "breakout_confirmed": True,
            },
        }

        result = FactorService._compute_ma100_low123_combined_factors(
            ma100_factors,
            pattern_123_factors,
            pattern_123_raw,
            group,
        )

        self.assertTrue(result["ma100_low123_confirmed"])
        self.assertFalse(result["ma100_low123_data_complete"])
        self.assertEqual(
            result["ma100_low123_validation_status"],
            "confirmed_missing_breakout_bar_index",
        )
        self.assertEqual(
            result["ma100_low123_validation_reason"],
            "missing_breakout_bar_index",
        )
        self.assertIn("缺少 breakout_bar_index", "".join(result["ma100_low123_hit_reasons"]))

    def test_trendline_breakout_index_does_not_replace_missing_p2_breakout_index(self):
        """趋势线突破时间不能替代真正的 P2 突破时间做 freshness 判定。"""
        group = self._make_raw_group()
        ma100_factors = {
            "above_ma100": True,
            "ma100_breakout_days": 2,
            "ma100": 95.0,
            "ma100_distance_pct": 1.8,
        }
        pattern_123_factors = {
            "pattern_123_low_trendline": True,
            "pattern_123_breakout_ready": True,
            "pattern_123_watchlist": False,
            "pattern_123_state": "breakout_ready",
            "pattern_123_entry_price": 98.5,
            "pattern_123_stop_loss": 92.0,
            "pattern_123_signal_strength": 0.84,
        }
        pattern_123_raw = {
            "downtrend_line": {
                "found": True,
                "touch_count": 3,
                "breakout_bar_index": 78,
                "breakout_confirmed": True,
            },
        }

        result = FactorService._compute_ma100_low123_combined_factors(
            ma100_factors,
            pattern_123_factors,
            pattern_123_raw,
            group,
        )

        self.assertTrue(result["ma100_low123_confirmed"])
        self.assertFalse(result["ma100_low123_data_complete"])
        self.assertEqual(
            result["ma100_low123_validation_status"],
            "confirmed_missing_breakout_bar_index",
        )
        self.assertEqual(
            result["ma100_low123_validation_reason"],
            "missing_breakout_bar_index",
        )


if __name__ == "__main__":
    unittest.main()
