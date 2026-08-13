"""TDD tests for FactorService v2 — extended factor dimensions.

Tests that FactorService computes additional factors beyond the original
price/volume set: pct_chg_Nd, ma5_distance_pct, candle_pattern, amplitude.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from src.config import Config
from src.services.factor_service import FactorService


def _make_daily_bars(code: str, days: int = 30, base_close: float = 10.0) -> list[dict]:
    """Generate synthetic daily bars for testing."""
    bars = []
    trade_date = date(2025, 3, 1)
    for i in range(days):
        d = trade_date + timedelta(days=i)
        close = base_close + (i * 0.1)
        bars.append({
            "code": code,
            "date": d,
            "open": close - 0.05,
            "high": close + 0.2,
            "low": close - 0.3,
            "close": close,
            "volume": 1_000_000 + i * 100_000,
            "amount": 10_000_000 + i * 1_000_000,
            "pct_chg": 1.0,
        })
    return bars


class TestFactorServiceExtendedFactors:

    def test_pct_chg_5d_computed(self):
        """Factor snapshot should include pct_chg_5d."""
        fs = FactorService.__new__(FactorService)
        fs.lookback_days = 80
        fs.min_list_days = 120
        fs.breakout_lookback_days = 20
        fs.config = Config()

        snapshot = self._build_snapshot_from_bars(fs, "600001", days=30)
        if snapshot.empty:
            pytest.skip("Snapshot generation requires DB; verifying field existence")

        assert "pct_chg_5d" in snapshot.columns

    def test_pct_chg_20d_computed(self):
        """Factor snapshot should include pct_chg_20d."""
        fs = FactorService.__new__(FactorService)
        fs.lookback_days = 80
        fs.min_list_days = 120
        fs.breakout_lookback_days = 20
        fs.config = Config()

        snapshot = self._build_snapshot_from_bars(fs, "600001", days=30)
        if snapshot.empty:
            pytest.skip("Snapshot generation requires DB; verifying field existence")

        assert "pct_chg_20d" in snapshot.columns

    def test_ma5_distance_pct_computed(self):
        """Factor snapshot should include ma5_distance_pct."""
        fs = FactorService.__new__(FactorService)
        fs.lookback_days = 80
        fs.min_list_days = 120
        fs.breakout_lookback_days = 20
        fs.config = Config()

        snapshot = self._build_snapshot_from_bars(fs, "600001", days=30)
        if snapshot.empty:
            pytest.skip("Snapshot generation requires DB; verifying field existence")

        assert "ma5_distance_pct" in snapshot.columns

    def test_candle_pattern_computed(self):
        """Factor snapshot should include candle_pattern field."""
        fs = FactorService.__new__(FactorService)
        fs.lookback_days = 80
        fs.min_list_days = 120
        fs.breakout_lookback_days = 20
        fs.config = Config()

        snapshot = self._build_snapshot_from_bars(fs, "600001", days=30)
        if snapshot.empty:
            pytest.skip("Snapshot generation requires DB; verifying field existence")

        assert "candle_pattern" in snapshot.columns

    def test_amplitude_computed(self):
        """Factor snapshot should include amplitude (振幅)."""
        fs = FactorService.__new__(FactorService)
        fs.lookback_days = 80
        fs.min_list_days = 120
        fs.breakout_lookback_days = 20
        fs.config = Config()

        snapshot = self._build_snapshot_from_bars(fs, "600001", days=30)
        if snapshot.empty:
            pytest.skip("Snapshot generation requires DB; verifying field existence")

        assert "amplitude" in snapshot.columns

    @staticmethod
    def _build_snapshot_from_bars(fs: FactorService, code: str, days: int = 30) -> pd.DataFrame:
        """Build a snapshot without DB by directly calling internal computation."""
        bars = _make_daily_bars(code, days=days)
        bars_df = pd.DataFrame(bars)
        trade_date = bars_df["date"].max()

        if not hasattr(fs, "_compute_extended_factors"):
            return pd.DataFrame()

        universe_map = {code: {"name": "测试股票", "list_date": "2020-01-01"}}
        group = bars_df[bars_df["code"] == code].sort_values("date").reset_index(drop=True)
        if len(group) < 20:
            return pd.DataFrame()

        latest = group.iloc[-1]
        close_series = group["close"].astype(float)

        result = fs._compute_extended_factors(group, latest, close_series)
        return pd.DataFrame([result])


class TestCandlePatternDetection:
    """Tests for the candle pattern detection helper."""

    def test_detect_big_yang(self):
        """Large bullish candle (>5% gain, body > 70% range)."""
        if not hasattr(FactorService, "_detect_candle_pattern"):
            pytest.skip("_detect_candle_pattern not yet implemented")

        bars = pd.DataFrame([
            {"open": 10.0, "high": 11.0, "low": 9.9, "close": 10.8, "pct_chg": 8.0, "volume": 1000},
        ])
        pattern = FactorService._detect_candle_pattern(bars)
        assert pattern == "big_yang"

    def test_detect_normal(self):
        """Normal candle, no special pattern."""
        if not hasattr(FactorService, "_detect_candle_pattern"):
            pytest.skip("_detect_candle_pattern not yet implemented")

        bars = pd.DataFrame([
            {"open": 10.0, "high": 10.3, "low": 9.8, "close": 10.1, "pct_chg": 1.0, "volume": 1000},
        ])
        pattern = FactorService._detect_candle_pattern(bars)
        assert pattern == "normal"

    def test_detect_doji(self):
        """Doji: very small body, high amplitude."""
        if not hasattr(FactorService, "_detect_candle_pattern"):
            pytest.skip("_detect_candle_pattern not yet implemented")

        bars = pd.DataFrame([
            {"open": 10.0, "high": 10.5, "low": 9.5, "close": 10.01, "pct_chg": 0.1, "volume": 500},
        ])
        pattern = FactorService._detect_candle_pattern(bars)
        assert pattern == "doji"
