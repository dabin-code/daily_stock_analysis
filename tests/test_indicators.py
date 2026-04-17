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


# ─────────────────────────────────────────────────────────────
# Factory helpers for gap/limit-up sub-semantics tests
# ─────────────────────────────────────────────────────────────
def _make_flat_then_gap_up_df(n: int = 30) -> pd.DataFrame:
    """Flat consolidation for n-1 bars, then a modest high-volume gap-up.

    The gap magnitude is kept small (~8%) so the synthetic fixture stays
    below the upgraded exhaustion-risk MA20-distance threshold (12%),
    isolating the breakaway-strict assertion from acceleration heuristics.
    """
    base = np.linspace(10.0, 10.1, n - 1)
    close = np.append(base, 10.9)
    high = close + 0.03
    low = close - 0.03
    # Force gap-up on the last bar: prev high ≈ 10.13; set last low above it.
    low[-1] = 10.7
    high[-1] = 10.95
    volume = np.full(n, 1_000_000, dtype=float)
    volume[-1] = 3_000_000
    pct_chg = np.concatenate([[0], np.diff(close) / close[:-1] * 100])
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "date": dates,
        "open": close - 0.03,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "pct_chg": pct_chg,
    })


def _make_exhaustion_gap_df(n: int = 110) -> pd.DataFrame:
    """Long slow uptrend then an accelerating rally + final gap-up."""
    slow = np.linspace(10.0, 13.0, n - 21)
    accel = np.linspace(13.0, 17.5, 20)
    close = np.concatenate([slow, accel, [19.5]])
    high = close + 0.1
    low = close - 0.1
    low[-1] = 18.0
    high[-1] = 19.6
    volume = np.full(n, 1_000_000, dtype=float)
    volume[-2] = 3_000_000
    volume[-1] = 4_000_000
    # Force last 5 bars to be big-yang (body ≥ 4%) so consecutive-big-yang
    # heuristic fires alongside the 20d rally condition.
    open_arr = close - 0.05
    for i in range(-5, 0):
        open_arr[i] = close[i] * 0.955
    pct_chg = np.concatenate([[0], np.diff(close) / close[:-1] * 100])
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "date": dates,
        "open": open_arr,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "pct_chg": pct_chg,
    })


def _make_continuation_gap_df(n: int = 80) -> pd.DataFrame:
    """Steady uptrend that recently had a gap-up, then a new small gap-up."""
    close = np.linspace(10.0, 12.0, n - 1)
    close = np.append(close, 12.3)
    high = close + 0.03
    low = close - 0.03
    low[-1] = 12.1
    volume = np.full(n, 1_000_000, dtype=float)
    volume[-1] = 1_600_000
    pct_chg = np.concatenate([[0], np.diff(close) / close[:-1] * 100])
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "date": dates,
        "open": close - 0.02,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "pct_chg": pct_chg,
    })


def _make_gap_then_retest_df(n: int = 30) -> pd.DataFrame:
    """Flat → gap-up 3 bars ago → price drifts back to gap upper edge on
    shrinking volume, held above the gap lower edge."""
    base = np.linspace(10.0, 10.1, n - 4)
    after_gap = np.array([12.0, 11.9, 11.7, 11.55])
    close = np.concatenate([base, after_gap])
    high = close + 0.03
    low = close - 0.03
    # gap-up bar: low must be > prev high
    gap_idx = len(base)
    low[gap_idx] = 11.5  # > 10.13
    high[gap_idx] = 12.1
    # retest bar (last): touch the gap upper edge (11.5 → gap_high=11.5),
    # close above gap lower edge (~10.13).
    low[-1] = 11.48
    high[-1] = 11.7
    volume = np.full(n, 1_000_000, dtype=float)
    volume[gap_idx] = 3_000_000
    volume[-1] = 600_000  # shrinking
    open_arr = close - 0.02
    pct_chg = np.concatenate([[0], np.diff(close) / close[:-1] * 100])
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "date": dates,
        "open": open_arr,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "pct_chg": pct_chg,
    })


def _make_structural_limit_up_df(n: int = 30) -> pd.DataFrame:
    """Flat consolidation then a first-board limit-up that closes above the
    prior 20-day high (no acceleration risk, no consecutive boards)."""
    close = np.linspace(10.0, 10.1, n - 1)
    close = np.append(close, close[-1] * 1.1)
    high = close.copy()
    low = close * 0.99
    high[-1] = close[-1]
    low[-1] = close[-1] * 0.98
    volume = np.full(n, 2_000_000, dtype=float)
    pct_chg = np.concatenate([[0], np.diff(close) / close[:-1] * 100])
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "date": dates,
        "open": close - 0.05,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "pct_chg": pct_chg,
    })


def _make_consecutive_limit_up_df(n: int = 30, count: int = 3) -> pd.DataFrame:
    """Tape with ``count`` back-to-back limit-ups at the end."""
    flat = np.linspace(10.0, 10.1, n - count)
    close = list(flat)
    for _ in range(count):
        close.append(close[-1] * 1.1)
    close = np.array(close)
    high = close.copy()
    low = close * 0.99
    volume = np.full(n, 2_000_000, dtype=float)
    pct_chg = np.concatenate([[0], np.diff(close) / close[:-1] * 100])
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "date": dates,
        "open": close - 0.05,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "pct_chg": pct_chg,
    })


# ─────────────────────────────────────────────────────────────
# GapDetector — sub-semantics
# ─────────────────────────────────────────────────────────────
class TestGapDetectorSubSemantics(unittest.TestCase):

    def test_breakaway_gap_returns_new_fields(self):
        from src.indicators.gap_detector import GapDetector
        df = _make_flat_then_gap_up_df()
        result = GapDetector.detect_breakaway_gap(df)
        for key in (
            "is_breakaway",
            "is_breakaway_strict",
            "exhaustion_risk_level",
            "exhaustion_risk_reasons",
            "gap_low",
            "gap_high",
            "broke_key_level",
            "key_level_type",
            "key_level_price",
        ):
            self.assertIn(key, result)

    def test_breakaway_strict_on_range_ceiling(self):
        from src.indicators.gap_detector import GapDetector
        df = _make_flat_then_gap_up_df()
        result = GapDetector.detect_breakaway_gap(df)
        self.assertTrue(result["is_breakaway"])  # legacy still fires
        self.assertTrue(result["is_breakaway_strict"])
        self.assertTrue(result["broke_key_level"])
        self.assertIn(result["key_level_type"], {"range_ceiling", "platform_box", "trendline"})
        self.assertGreater(result["key_level_price"], 0.0)

    def test_legacy_is_breakaway_stable_on_exhaustion(self):
        """20d-rally > 30% should still veto the legacy ``is_breakaway``
        flag so downstream leader-score / hot-theme consumers see no
        regression from the sub-semantic refactor."""
        from src.indicators.gap_detector import GapDetector
        df = _make_exhaustion_gap_df()
        result = GapDetector.detect_breakaway_gap(df)
        self.assertTrue(result["is_gap_up"])
        self.assertTrue(result["is_exhaustion_risk"])  # legacy flag
        self.assertFalse(result["is_breakaway"])  # legacy breakaway blocked
        self.assertFalse(result["is_breakaway_strict"])  # strict blocked too

    def test_exhaustion_risk_level_high_has_multiple_reasons(self):
        from src.indicators.gap_detector import GapDetector
        df = _make_exhaustion_gap_df()
        result = GapDetector.detect_breakaway_gap(df)
        self.assertEqual(result["exhaustion_risk_level"], "high")
        self.assertGreaterEqual(len(result["exhaustion_risk_reasons"]), 2)

    def test_continuation_gap_true_in_uptrend(self):
        from src.indicators.gap_detector import GapDetector
        df = _make_continuation_gap_df()
        result = GapDetector.detect_continuation_gap(df)
        self.assertTrue(result["is_continuation"])

    def test_continuation_gap_false_when_not_gap_up(self):
        from src.indicators.gap_detector import GapDetector
        df = _make_continuation_gap_df()
        # Remove the gap: bring last low inside prev bar's range.
        df.loc[df.index[-1], "low"] = df["high"].iloc[-2] - 0.05
        result = GapDetector.detect_continuation_gap(df)
        self.assertFalse(result["is_continuation"])

    def test_gap_retest_hold_true(self):
        from src.indicators.gap_detector import GapDetector
        df = _make_gap_then_retest_df()
        result = GapDetector.detect_gap_retest_hold(df)
        self.assertTrue(result["retest_hold"])
        self.assertGreaterEqual(result["bars_since_event"], 1)
        self.assertTrue(result["touched_upper_edge"])
        self.assertTrue(result["held_above_gap_low"])
        self.assertTrue(result["volume_shrinking"])

    def test_locate_recent_breakaway_gap(self):
        from src.indicators.gap_detector import GapDetector
        df = _make_gap_then_retest_df()
        locate = GapDetector.locate_recent_breakaway_gap(df, lookback=8)
        self.assertGreaterEqual(locate["event_bar_index"], 0)
        self.assertGreaterEqual(locate["bars_since_event"], 0)
        self.assertGreater(locate["gap_low"], 0.0)

    def test_gap_retest_hold_invalidated_when_gap_filled(self):
        """A close that breaks back below ``gap_low`` must void retest_hold.

        Plan acceptance scenario: "缺口被回补导致信号失效" — once the
        pullback violates the gap's lower edge, the second-entry buy
        point cannot stand anymore.
        """
        from src.indicators.gap_detector import GapDetector
        df = _make_gap_then_retest_df().copy()
        # Collapse the latest bar below the gap lower edge (~10.13).
        df.loc[df.index[-1], "low"] = 9.90
        df.loc[df.index[-1], "close"] = 9.95
        df.loc[df.index[-1], "open"] = 10.80
        df.loc[df.index[-1], "high"] = 10.85
        result = GapDetector.detect_gap_retest_hold(df)
        self.assertFalse(result["retest_hold"])
        self.assertFalse(result["held_above_gap_low"])


# ─────────────────────────────────────────────────────────────
# LimitUpDetector — sub-semantics
# ─────────────────────────────────────────────────────────────
class TestLimitUpDetectorSubSemantics(unittest.TestCase):

    def test_breakout_limit_up_returns_new_fields(self):
        from src.indicators.limit_up_detector import LimitUpDetector
        df = _make_structural_limit_up_df()
        result = LimitUpDetector.is_breakout_limit_up(df)
        for key in (
            "is_structural_breakout",
            "key_level_type",
            "key_level_price",
            "consecutive_limit_up_count",
            "is_first_board",
            "high_acceleration_risk",
            "high_acceleration_reasons",
        ):
            self.assertIn(key, result)

    def test_structural_breakout_on_first_board(self):
        from src.indicators.limit_up_detector import LimitUpDetector
        df = _make_structural_limit_up_df()
        result = LimitUpDetector.is_breakout_limit_up(df)
        self.assertTrue(result["is_limit_up"])
        self.assertTrue(result["is_breakout_high"])
        self.assertTrue(result["is_structural_breakout"])
        self.assertEqual(result["consecutive_limit_up_count"], 1)
        self.assertTrue(result["is_first_board"])
        self.assertFalse(result["high_acceleration_risk"])

    def test_high_acceleration_risk_on_third_board(self):
        from src.indicators.limit_up_detector import LimitUpDetector
        df = _make_consecutive_limit_up_df(count=3)
        result = LimitUpDetector.is_breakout_limit_up(df)
        self.assertTrue(result["is_limit_up"])
        self.assertEqual(result["consecutive_limit_up_count"], 3)
        self.assertFalse(result["is_first_board"])
        self.assertTrue(result["high_acceleration_risk"])
        # Structural breakout must be vetoed by acceleration risk even if
        # the close is above the prior range.
        self.assertFalse(result["is_structural_breakout"])

    def test_locate_recent_structural_limitup(self):
        from src.indicators.limit_up_detector import LimitUpDetector
        df = _make_structural_limit_up_df(n=30)
        locate = LimitUpDetector.locate_recent_structural_limitup(df, lookback=5)
        self.assertGreaterEqual(locate["event_bar_index"], 0)
        self.assertGreaterEqual(locate["bars_since_event"], 0)


if __name__ == "__main__":
    unittest.main()
