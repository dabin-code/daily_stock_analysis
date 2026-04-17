# -*- coding: utf-8 -*-
"""Integration tests for ``FactorService._compute_gap_limit_factors``.

Covers the sub-semantic split introduced for the ``gap_limitup_breakout``
strategy review:

- Legacy keys preserved (``gap_up``, ``gap_breakaway``,
  ``gap_exhaustion_risk``, ``is_limit_up``, ``limit_up_breakout``).
- New breakaway / continuation / retest / limitup structural keys are
  populated with sensible defaults.
- Derived gating fields (``bars_since_event``,
  ``has_recent_breakaway_event``, ``near_recent_rally_peak``,
  ``retest_type``) behave as designed.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.services.factor_service import FactorService


# Reuse the factory helpers from the detector test module to keep fixtures
# aligned.  Importing them avoids duplicating fragile numeric setups.
from tests.test_indicators import (  # noqa: E402
    _make_flat_then_gap_up_df,
    _make_exhaustion_gap_df,
    _make_gap_then_retest_df,
    _make_structural_limit_up_df,
    _make_consecutive_limit_up_df,
)


def test_factors_on_empty_frame_returns_legacy_defaults():
    result = FactorService._compute_gap_limit_factors(pd.DataFrame())
    for key in (
        "gap_up",
        "gap_breakaway",
        "gap_exhaustion_risk",
        "is_limit_up",
        "limit_up_breakout",
    ):
        assert key in result
        assert result[key] is False


def test_factors_preserve_legacy_keys_on_flat_gap():
    df = _make_flat_then_gap_up_df()
    result = FactorService._compute_gap_limit_factors(df)
    assert result["gap_up"] is True
    assert result["gap_breakaway"] is True  # legacy threshold satisfied
    assert result["gap_exhaustion_risk"] is False
    # Strict breakaway additionally requires key-level break.
    assert result["breakaway_gap_strict"] is True
    assert result["gap_broke_key_level"] is True
    assert result["gap_key_level_type"] in {"range_ceiling", "platform_box", "trendline"}
    assert result["gap_key_level_price"] > 0


def test_factors_exhaustion_risk_upgraded_level():
    df = _make_exhaustion_gap_df()
    result = FactorService._compute_gap_limit_factors(df)
    assert result["gap_exhaustion_risk"] is True  # legacy flag preserved
    assert result["gap_exhaustion_risk_level"] == "high"
    assert len(result["gap_exhaustion_risk_reasons"]) >= 2
    assert result["breakaway_gap_strict"] is False


def test_factors_retest_hold_maps_to_gap_support():
    df = _make_gap_then_retest_df()
    result = FactorService._compute_gap_limit_factors(df)
    assert result["retest_hold"] is True
    assert result["retest_type"] == "gap_support"
    assert result["retest_support_price"] > 0
    # bars_since_event tracks the original gap-up event, not the retest
    # bar itself.
    assert result["bars_since_event"] >= 1
    assert result["has_recent_breakaway_event"] is True


def test_factors_structural_limit_up_first_board():
    df = _make_structural_limit_up_df()
    result = FactorService._compute_gap_limit_factors(df)
    assert result["is_limit_up"] is True
    assert result["limit_up_breakout"] is True
    assert result["limitup_structure_breakout"] is True
    assert result["limitup_consecutive_count"] == 1
    assert result["limitup_is_first_board"] is True
    assert result["limitup_high_acceleration_risk"] is False
    assert result["bars_since_limitup_structure_breakout"] == 0
    assert result["bars_since_event"] == 0
    assert result["has_recent_breakaway_event"] is True


def test_factors_consecutive_boards_trigger_high_acceleration_risk():
    df = _make_consecutive_limit_up_df(count=3)
    result = FactorService._compute_gap_limit_factors(df)
    assert result["is_limit_up"] is True
    assert result["limitup_consecutive_count"] == 3
    assert result["limitup_high_acceleration_risk"] is True
    assert result["limitup_structure_breakout"] is False


def test_factors_near_rally_peak_flag():
    """A tape that closes near its recent 20-bar high should flag
    ``near_recent_rally_peak=True`` regardless of gap / limit-up events."""
    n = 40
    close = np.concatenate([
        np.linspace(10.0, 12.0, n - 1),
        [12.0],  # sits right at recent peak
    ])
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    df = pd.DataFrame({
        "date": dates,
        "open": close - 0.02,
        "high": close + 0.02,
        "low": close - 0.02,
        "close": close,
        "volume": np.full(n, 1_000_000, dtype=float),
        "pct_chg": np.concatenate([[0], np.diff(close) / close[:-1] * 100]),
    })
    result = FactorService._compute_gap_limit_factors(df)
    assert result["near_recent_rally_peak"] is True
