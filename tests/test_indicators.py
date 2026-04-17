# -*- coding: utf-8 -*-
"""
TDD tests for Phase 1c: Indicator detectors.

1. MABreakoutDetector — breakout + pullback support detection
2. GapDetector — gap detection + breakaway gap identification
3. LimitUpDetector — limit-up detection + breakout-high identification
"""

import unittest
import numpy as np
import pandas as pd


def _make_df(n: int = 60, base: float = 10.0, trend: float = 0.002) -> pd.DataFrame:
    np.random.seed(42)
    prices = [base]
    for _ in range(n - 1):
        change = np.random.randn() * 0.01 + trend
        prices.append(prices[-1] * (1 + change))
    close = np.array(prices)
    dates = pd.date_range(start="2025-01-01", periods=n, freq="D")
    high = close * (1 + np.random.uniform(0, 0.02, n))
    low = close * (1 - np.random.uniform(0, 0.02, n))
    return pd.DataFrame({
        "date": dates,
        "open": close * (1 - np.random.uniform(-0.005, 0.005, n)),
        "high": high,
        "low": low,
        "close": close,
        "volume": np.random.randint(1_000_000, 5_000_000, n),
        "pct_chg": np.concatenate([[0], np.diff(close) / close[:-1] * 100]),
    })


def _make_gap_up_df() -> pd.DataFrame:
    """Create a DataFrame where the last day gaps up (low > prev high)."""
    n = 30
    np.random.seed(42)
    close = np.linspace(10.0, 11.0, n)
    high = close + 0.1
    low = close - 0.1
    # Force gap: last day's low > previous day's high
    close[-1] = 12.0
    high[-1] = 12.5
    low[-1] = 11.3  # > high[-2] = 11.1
    dates = pd.date_range(start="2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "date": dates,
        "open": close - 0.05,
        "high": high,
        "low": low,
        "close": close,
        "volume": np.full(n, 2_000_000),
        "pct_chg": np.concatenate([[0], np.diff(close) / close[:-1] * 100]),
    })


def _make_limit_up_df() -> pd.DataFrame:
    """Create a DataFrame where the last day is limit up (+10%)."""
    n = 30
    close = np.linspace(10.0, 11.0, n)
    close[-1] = close[-2] * 1.1  # +10%
    high = close.copy()
    high[-1] = close[-1]
    low = close - 0.1
    low[-1] = close[-1] * 0.98
    pct = np.concatenate([[0], np.diff(close) / close[:-1] * 100])
    dates = pd.date_range(start="2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "date": dates,
        "open": close - 0.05,
        "high": high,
        "low": low,
        "close": close,
        "volume": np.full(n, 3_000_000),
        "pct_chg": pct,
    })


# ─────────────────────────────────────────────────────────────
# MABreakoutDetector
# ─────────────────────────────────────────────────────────────
class TestMABreakoutDetector(unittest.TestCase):

    def test_detect_breakout_returns_dict(self):
        from src.indicators.ma_breakout_detector import MABreakoutDetector
        df = _make_df(n=120)
        result = MABreakoutDetector.detect_breakout(df, ma_period=100)
        self.assertIsInstance(result, dict)
        self.assertIn("is_breakout", result)
        self.assertIn("breakout_days", result)

    def test_breakout_in_uptrend(self):
        from src.indicators.ma_breakout_detector import MABreakoutDetector
        df = _make_df(n=120, trend=0.003)
        result = MABreakoutDetector.detect_breakout(df, ma_period=20)
        self.assertTrue(result["is_breakout"])
        self.assertGreater(result["breakout_days"], 0)

    def test_no_breakout_in_downtrend(self):
        from src.indicators.ma_breakout_detector import MABreakoutDetector
        df = _make_df(n=120, trend=-0.003)
        result = MABreakoutDetector.detect_breakout(df, ma_period=20)
        self.assertFalse(result["is_breakout"])

    def test_detect_pullback_support(self):
        from src.indicators.ma_breakout_detector import MABreakoutDetector
        df = _make_df(n=120, trend=0.002)
        result = MABreakoutDetector.detect_pullback_support(df, ma_period=20)
        self.assertIsInstance(result, dict)
        self.assertIn("is_pullback_support", result)

    def test_insufficient_data(self):
        from src.indicators.ma_breakout_detector import MABreakoutDetector
        df = _make_df(n=10)
        result = MABreakoutDetector.detect_breakout(df, ma_period=20)
        self.assertFalse(result["is_breakout"])

    def test_output_includes_real_crossing_fields(self):
        """detect_breakout must expose the new real-crossing semantic fields."""
        from src.indicators.ma_breakout_detector import MABreakoutDetector
        df = _make_df(n=120)
        result = MABreakoutDetector.detect_breakout(df, ma_period=100)
        self.assertIn("breakout_bar_index", result)
        self.assertIn("bars_since_breakout", result)
        self.assertIn("pre_breakout_below_ratio", result)
        self.assertIn("pre_breakout_consecutive_below_bars", result)

    def test_real_crossing_after_long_below_is_detected(self):
        """Build a tape that sits below MA20 for 20 bars then crosses up on
        the latest bar; detector must report bars_since_breakout=0 and a
        pre-breakout context that clearly flags the below-MA regime."""
        from src.indicators.ma_breakout_detector import MABreakoutDetector
        n = 40
        close = np.concatenate([
            np.linspace(10.0, 9.0, n - 1),  # long downtrend / below-MA regime
            [11.0],  # sharp cross-up on latest bar
        ])
        dates = pd.date_range(start="2025-01-01", periods=n, freq="D")
        df = pd.DataFrame({
            "date": dates,
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": np.full(n, 1_000_000),
            "pct_chg": np.concatenate([[0], np.diff(close) / close[:-1] * 100]),
        })
        result = MABreakoutDetector.detect_breakout(df, ma_period=20)
        self.assertTrue(result["is_breakout"])
        self.assertEqual(result["bars_since_breakout"], 0)
        self.assertEqual(result["breakout_bar_index"], n - 1)
        self.assertGreaterEqual(result["pre_breakout_below_ratio"], 0.6)
        self.assertGreaterEqual(result["pre_breakout_consecutive_below_bars"], 3)

    def test_no_real_crossing_when_always_above_ma(self):
        """Uptrend that never dips below MA: breakout_days is large but
        no real upward crossing exists → bars_since_breakout is None."""
        from src.indicators.ma_breakout_detector import MABreakoutDetector
        n = 60
        close = np.linspace(10.0, 20.0, n)  # strictly monotonic, always above any MA20
        dates = pd.date_range(start="2025-01-01", periods=n, freq="D")
        df = pd.DataFrame({
            "date": dates,
            "open": close,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": np.full(n, 1_000_000),
            "pct_chg": np.concatenate([[0], np.diff(close) / close[:-1] * 100]),
        })
        result = MABreakoutDetector.detect_breakout(df, ma_period=20)
        self.assertTrue(result["is_breakout"])
        self.assertGreater(result["breakout_days"], 0)
        self.assertIsNone(result["bars_since_breakout"])
        self.assertIsNone(result["breakout_bar_index"])


# ─────────────────────────────────────────────────────────────
# GapDetector
# ─────────────────────────────────────────────────────────────
class TestGapDetector(unittest.TestCase):

    def test_detect_gaps_returns_list(self):
        from src.indicators.gap_detector import GapDetector
        df = _make_df(n=60)
        gaps = GapDetector.detect_gaps(df)
        self.assertIsInstance(gaps, list)

    def test_detect_gap_up(self):
        from src.indicators.gap_detector import GapDetector
        df = _make_gap_up_df()
        gaps = GapDetector.detect_gaps(df)
        up_gaps = [g for g in gaps if g["direction"] == "up"]
        self.assertGreater(len(up_gaps), 0)

    def test_detect_breakaway_gap(self):
        from src.indicators.gap_detector import GapDetector
        df = _make_gap_up_df()
        result = GapDetector.detect_breakaway_gap(df)
        self.assertIsInstance(result, dict)
        self.assertIn("is_breakaway", result)

    def test_gap_has_required_fields(self):
        from src.indicators.gap_detector import GapDetector
        df = _make_gap_up_df()
        gaps = GapDetector.detect_gaps(df)
        if gaps:
            g = gaps[0]
            self.assertIn("date", g)
            self.assertIn("direction", g)
            self.assertIn("gap_low", g)
            self.assertIn("gap_high", g)


# ─────────────────────────────────────────────────────────────
# LimitUpDetector
# ─────────────────────────────────────────────────────────────
class TestLimitUpDetector(unittest.TestCase):

    def test_is_limit_up_positive(self):
        from src.indicators.limit_up_detector import LimitUpDetector
        self.assertTrue(LimitUpDetector.is_limit_up(pct_chg=9.95))

    def test_is_limit_up_negative(self):
        from src.indicators.limit_up_detector import LimitUpDetector
        self.assertFalse(LimitUpDetector.is_limit_up(pct_chg=5.0))

    def test_is_limit_up_star_board(self):
        """Star Market (科创板) has 20% limit."""
        from src.indicators.limit_up_detector import LimitUpDetector
        self.assertTrue(LimitUpDetector.is_limit_up(pct_chg=19.9, board="star"))
        self.assertFalse(LimitUpDetector.is_limit_up(pct_chg=15.0, board="star"))

    def test_is_breakout_limit_up(self):
        from src.indicators.limit_up_detector import LimitUpDetector
        df = _make_limit_up_df()
        result = LimitUpDetector.is_breakout_limit_up(df)
        self.assertIsInstance(result, dict)
        self.assertIn("is_limit_up", result)
        self.assertIn("is_breakout_high", result)

    def test_limit_up_with_breakout_high(self):
        from src.indicators.limit_up_detector import LimitUpDetector
        df = _make_limit_up_df()
        # Make the limit-up close higher than all previous highs
        df.loc[df.index[-1], "close"] = df["high"].iloc[:-1].max() * 1.05
        df.loc[df.index[-1], "high"] = df.loc[df.index[-1], "close"]
        df.loc[df.index[-1], "pct_chg"] = 10.0
        result = LimitUpDetector.is_breakout_limit_up(df)
        self.assertTrue(result["is_limit_up"])
        self.assertTrue(result["is_breakout_high"])


if __name__ == "__main__":
    unittest.main()
