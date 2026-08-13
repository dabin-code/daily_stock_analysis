# -*- coding: utf-8 -*-
"""
TDD tests for Phase 2f: FactorService MACD divergence factor.

Tests cover:
1. _compute_macd_divergence_factors returns expected keys
2. macd_bull_divergence and macd_bear_divergence are boolean
3. Factors are integrated into _compute_extended_factors output
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


class TestComputeMACDDivergenceFactors(unittest.TestCase):
    """FactorService._compute_macd_divergence_factors static method."""

    def test_returns_expected_keys(self):
        from src.services.factor_service import FactorService
        group = _make_group_df()
        result = FactorService._compute_macd_divergence_factors(group)
        self.assertIn("macd_bull_divergence", result)
        self.assertIn("macd_bear_divergence", result)

    def test_values_are_boolean(self):
        from src.services.factor_service import FactorService
        group = _make_group_df()
        result = FactorService._compute_macd_divergence_factors(group)
        self.assertIsInstance(result["macd_bull_divergence"], bool)
        self.assertIsInstance(result["macd_bear_divergence"], bool)

    def test_short_data_returns_false(self):
        from src.services.factor_service import FactorService
        short = _make_group_df(n=10)
        result = FactorService._compute_macd_divergence_factors(short)
        self.assertFalse(result["macd_bull_divergence"])
        self.assertFalse(result["macd_bear_divergence"])


class TestExtendedFactorsIncludeMACD(unittest.TestCase):
    """_compute_extended_factors merges MACD divergence factors."""

    def test_macd_keys_in_extended(self):
        from src.services.factor_service import FactorService
        svc = FactorService.__new__(FactorService)
        svc.config = Config()
        group = _make_group_df()
        latest = group.iloc[-1]
        close_series = group["close"]
        result = svc._compute_extended_factors(group, latest, close_series)
        self.assertIn("macd_bull_divergence", result)
        self.assertIn("macd_bear_divergence", result)


if __name__ == "__main__":
    unittest.main()
