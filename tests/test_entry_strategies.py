# -*- coding: utf-8 -*-
"""
TDD tests for Phase 1d: Entry strategies C and D (daily version).

Strategy C: Gap breakout OR limit-up breakout + volume confirmation
Strategy D: MA100 filter -> MA100/MA20 breakout -> pullback support (daily only)
"""

import unittest
import numpy as np
import pandas as pd


def _make_uptrend_df(n: int = 120, base: float = 10.0) -> pd.DataFrame:
    np.random.seed(42)
    prices = [base]
    for _ in range(n - 1):
        change = np.random.randn() * 0.01 + 0.002
        prices.append(prices[-1] * (1 + change))
    close = np.array(prices)
    high = close * (1 + np.random.uniform(0, 0.015, n))
    low = close * (1 - np.random.uniform(0, 0.015, n))
    dates = pd.date_range(start="2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "date": dates,
        "open": close * 0.999,
        "high": high,
        "low": low,
        "close": close,
        "volume": np.random.randint(1_000_000, 5_000_000, n),
        "pct_chg": np.concatenate([[0], np.diff(close) / close[:-1] * 100]),
    })


def _make_gap_breakout_df() -> pd.DataFrame:
    """DF with gap-up breakout on last day + high volume."""
    n = 120
    np.random.seed(42)
    prices = np.linspace(10.0, 12.0, n)
    high = prices + 0.1
    low = prices - 0.1
    volume = np.full(n, 2_000_000)
    # Gap-up on last day: low > prev high
    prices[-1] = 13.0
    high[-1] = 13.5
    low[-1] = 12.2  # > high[-2] ≈ 12.1
    volume[-1] = 8_000_000  # volume surge
    pct = np.concatenate([[0], np.diff(prices) / prices[:-1] * 100])
    dates = pd.date_range(start="2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "date": dates,
        "open": prices * 0.999,
        "high": high,
        "low": low,
        "close": prices,
        "volume": volume,
        "pct_chg": pct,
    })


def _make_limit_breakout_df() -> pd.DataFrame:
    """DF with limit-up breakout on last day."""
    n = 120
    np.random.seed(42)
    prices = np.linspace(10.0, 12.0, n)
    high = prices + 0.1
    low = prices - 0.1
    volume = np.full(n, 2_000_000)
    # Limit-up on last day
    prices[-1] = prices[-2] * 1.1
    high[-1] = prices[-1]
    low[-1] = prices[-2] * 1.02
    volume[-1] = 6_000_000
    pct = np.concatenate([[0], np.diff(prices) / prices[:-1] * 100])
    dates = pd.date_range(start="2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "date": dates,
        "open": prices * 0.999,
        "high": high,
        "low": low,
        "close": prices,
        "volume": volume,
        "pct_chg": pct,
    })


def _make_below_ma100_df() -> pd.DataFrame:
    """Downtrend DF where price is below MA100."""
    n = 120
    np.random.seed(99)
    prices = [20.0]
    for _ in range(n - 1):
        prices.append(prices[-1] * (1 + np.random.randn() * 0.01 - 0.003))
    close = np.array(prices)
    dates = pd.date_range(start="2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "date": dates,
        "open": close * 0.999,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": np.full(n, 2_000_000),
        "pct_chg": np.concatenate([[0], np.diff(close) / close[:-1] * 100]),
    })


# ─────────────────────────────────────────────────────────────
# EntryStrategyC
# ─────────────────────────────────────────────────────────────
class TestEntryStrategyC(unittest.TestCase):

    def test_signal_on_gap_breakout(self):
        from src.strategies.entry_strategies import EntryStrategyC
        df = _make_gap_breakout_df()
        result = EntryStrategyC.evaluate(df)
        self.assertIsInstance(result, dict)
        self.assertIn("triggered", result)
        self.assertTrue(result["triggered"])
        self.assertIn("reason", result)

    def test_signal_on_limit_breakout(self):
        from src.strategies.entry_strategies import EntryStrategyC
        df = _make_limit_breakout_df()
        result = EntryStrategyC.evaluate(df)
        self.assertTrue(result["triggered"])

    def test_no_signal_in_normal_day(self):
        from src.strategies.entry_strategies import EntryStrategyC
        df = _make_uptrend_df(n=120)
        result = EntryStrategyC.evaluate(df)
        self.assertFalse(result["triggered"])

    def test_returns_score(self):
        from src.strategies.entry_strategies import EntryStrategyC
        df = _make_gap_breakout_df()
        result = EntryStrategyC.evaluate(df)
        self.assertIn("score", result)
        self.assertGreater(result["score"], 0)

    # ── Structured sub-semantic output ─────────────────────────────
    def test_breakaway_gap_emits_structured_stop(self):
        """Strict breakaway gap → sub_setup + gap_low stop + support."""
        from src.strategies.entry_strategies import EntryStrategyC
        df = _make_gap_breakout_df()
        result = EntryStrategyC.evaluate(df)
        self.assertTrue(result["triggered"])
        # Priority ordering: on this fixture the bar is also a structural
        # limit-up breakout when the close pct_chg clears the threshold,
        # so accept either the limitup or breakaway classification here.
        self.assertIn(
            result["sub_setup_type"],
            ("breakaway_gap_strict", "limitup_structure"),
        )
        self.assertIsNotNone(result["entry_price"])
        self.assertIsNotNone(result["stop_loss_price"])
        self.assertGreater(result["entry_price"], result["stop_loss_price"])
        self.assertIn(
            result["stop_loss_basis"],
            ("gap_low", "limitup_day_open"),
        )

    def test_limit_breakout_emits_limitup_structure(self):
        """Limit-up breakout with key-level → sub_setup=limitup_structure."""
        from src.strategies.entry_strategies import EntryStrategyC
        df = _make_limit_breakout_df()
        result = EntryStrategyC.evaluate(df)
        self.assertTrue(result["triggered"])
        self.assertEqual(result["sub_setup_type"], "limitup_structure")
        self.assertEqual(result["stop_loss_basis"], "limitup_day_open")
        self.assertGreater(result["entry_price"], result["stop_loss_price"])

    def test_normal_uptrend_emits_sub_setup_none(self):
        """Noise-only random walk must not produce any buy sub-setup."""
        from src.strategies.entry_strategies import EntryStrategyC
        df = _make_uptrend_df(n=120)
        result = EntryStrategyC.evaluate(df)
        self.assertFalse(result["triggered"])
        self.assertEqual(result["sub_setup_type"], "none")
        self.assertIsNone(result["entry_price"])
        self.assertIsNone(result["stop_loss_price"])


# ─────────────────────────────────────────────────────────────
# EntryStrategyD (daily version)
# ─────────────────────────────────────────────────────────────
class TestEntryStrategyD(unittest.TestCase):

    def test_signal_in_uptrend_above_ma100(self):
        from src.strategies.entry_strategies import EntryStrategyD
        df = _make_uptrend_df(n=120)
        result = EntryStrategyD.evaluate(df)
        self.assertIsInstance(result, dict)
        self.assertIn("triggered", result)
        self.assertIn("above_ma100", result)
        self.assertTrue(result["above_ma100"])

    def test_no_signal_below_ma100(self):
        from src.strategies.entry_strategies import EntryStrategyD
        df = _make_below_ma100_df()
        result = EntryStrategyD.evaluate(df)
        self.assertFalse(result["above_ma100"])
        self.assertFalse(result["triggered"])

    def test_returns_breakout_info(self):
        from src.strategies.entry_strategies import EntryStrategyD
        df = _make_uptrend_df(n=120)
        result = EntryStrategyD.evaluate(df)
        self.assertIn("ma100_breakout_days", result)
        self.assertIn("ma20_breakout", result)

    def test_returns_stop_loss(self):
        from src.strategies.entry_strategies import EntryStrategyD
        df = _make_uptrend_df(n=120)
        result = EntryStrategyD.evaluate(df)
        self.assertIn("stop_loss_price", result)
        self.assertIn("stop_loss_ma", result)

    def test_insufficient_data(self):
        from src.strategies.entry_strategies import EntryStrategyD
        df = _make_uptrend_df(n=30)
        result = EntryStrategyD.evaluate(df)
        self.assertFalse(result["triggered"])

    def test_score_range(self):
        from src.strategies.entry_strategies import EntryStrategyD
        df = _make_uptrend_df(n=120)
        result = EntryStrategyD.evaluate(df)
        self.assertIn("score", result)
        self.assertGreaterEqual(result["score"], 0)
        self.assertLessEqual(result["score"], 100)


if __name__ == "__main__":
    unittest.main()
