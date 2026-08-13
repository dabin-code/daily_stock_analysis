# -*- coding: utf-8 -*-
"""
TDD tests for Phase 3f: FactorService trendline + 123 pattern factors.

Tests cover:
1. _compute_trendline_factors returns expected keys
2. _compute_pattern_123_factors returns expected keys
3. Factors integrated into _compute_extended_factors
"""

import unittest
import numpy as np
import pandas as pd

from src.config import Config


def _make_group_df(n: int = 120) -> pd.DataFrame:
    np.random.seed(42)
    prices = np.cumsum(np.random.randn(n) * 0.3) + 20.0
    return pd.DataFrame({
        "date": pd.date_range(end="2025-12-31", periods=n, freq="B"),
        "open": prices - 0.1,
        "high": prices + 0.2,
        "low": prices - 0.2,
        "close": prices,
        "volume": np.random.randint(100000, 500000, n).astype(float),
        "amount": np.random.randint(1000000, 5000000, n).astype(float),
        "pct_chg": np.random.randn(n) * 1.0,
    })


class TestComputeTrendlineFactors(unittest.TestCase):
    def test_returns_expected_keys(self):
        from src.services.factor_service import FactorService
        group = _make_group_df()
        result = FactorService._compute_trendline_factors(group)
        self.assertIn("trendline_breakout", result)
        self.assertIn("trendline_touch_count", result)

    def test_values_are_correct_types(self):
        from src.services.factor_service import FactorService
        group = _make_group_df()
        result = FactorService._compute_trendline_factors(group)
        self.assertIsInstance(result["trendline_breakout"], bool)
        self.assertIsInstance(result["trendline_touch_count"], int)

    def test_short_data(self):
        from src.services.factor_service import FactorService
        short = _make_group_df(n=10)
        result = FactorService._compute_trendline_factors(short)
        self.assertFalse(result["trendline_breakout"])


class TestComputePattern123Factors(unittest.TestCase):
    def test_returns_expected_keys(self):
        from src.services.factor_service import FactorService
        group = _make_group_df()
        result, _raw = FactorService._compute_pattern_123_factors(group, Config())
        self.assertIn("pattern_123_bottom", result)
        self.assertIn("pattern_123_breakout", result)
        self.assertIn("pattern_123_higher_low_pct", result)
        self.assertIn("pattern_123_watchlist", result)
        self.assertIn("pattern_123_breakout_ready", result)
        self.assertIn("pattern_123_state", result)

    def test_values_are_correct_types(self):
        from src.services.factor_service import FactorService
        group = _make_group_df()
        result, _raw = FactorService._compute_pattern_123_factors(group, Config())
        self.assertIsInstance(result["pattern_123_bottom"], bool)
        self.assertIsInstance(result["pattern_123_breakout"], bool)
        self.assertIsInstance(result["pattern_123_higher_low_pct"], float)
        self.assertIsInstance(result["pattern_123_watchlist"], bool)
        self.assertIsInstance(result["pattern_123_breakout_ready"], bool)


class TestExtendedFactorsIncludePhase3(unittest.TestCase):
    def test_keys_in_extended(self):
        from src.services.factor_service import FactorService
        svc = FactorService.__new__(FactorService)
        svc.config = Config()
        group = _make_group_df()
        latest = group.iloc[-1]
        close_series = group["close"]
        result = svc._compute_extended_factors(group, latest, close_series)
        self.assertIn("trendline_breakout", result)
        self.assertIn("pattern_123_bottom", result)
        self.assertIn("pattern_123_breakout", result)


if __name__ == "__main__":
    unittest.main()
