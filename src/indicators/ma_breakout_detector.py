# -*- coding: utf-8 -*-
"""
MA Breakout Detector — detects price breakout above a moving average and
pullback-to-support patterns.

Semantic fields returned by :meth:`detect_breakout`:

- ``is_breakout`` / ``breakout_days`` / ``ma_value`` / ``price`` / ``distance_pct``
  — *legacy* outputs kept for backward compatibility.  ``breakout_days`` counts
  the number of consecutive bars (from the latest bar backward) whose close is
  above the MA.  It does **not** measure "bars since a real upward crossing".

- ``breakout_bar_index`` / ``bars_since_breakout`` — the index (and age, in
  bars) of the most recent **real upward crossing**, defined as the first bar
  ``i`` (scanning latest-to-earliest) where ``close[i-1] <= MA[i-1]`` and
  ``close[i] > MA[i]``.  ``bars_since_breakout == 0`` means the crossing
  happened on the latest bar.  ``None`` when no such crossing is found within
  the available data window.

- ``pre_breakout_below_ratio`` / ``pre_breakout_consecutive_below_bars`` —
  background context immediately before the detected crossing: the fraction of
  pre-breakout bars (within ``pre_breakout_window``) whose close was at-or-below
  the MA, and the number of consecutive bars immediately before the crossing
  that closed at-or-below the MA.  Callers use these to filter out "noise
  flips" from a stock that was already running above MA100.
"""

from typing import Dict, Any, Optional

import numpy as np
import pandas as pd


class MABreakoutDetector:
    """Stateless detector for MA breakout and pullback-to-support."""

    SUPPORT_TOLERANCE = 0.02
    DEFAULT_PRE_BREAKOUT_WINDOW = 20

    @classmethod
    def _empty_breakout_result(cls, ma_value: float = 0.0) -> Dict[str, Any]:
        return {
            "is_breakout": False,
            "breakout_days": 0,
            "ma_value": ma_value,
            "breakout_bar_index": None,
            "bars_since_breakout": None,
            "pre_breakout_below_ratio": 0.0,
            "pre_breakout_consecutive_below_bars": 0,
        }

    @classmethod
    def detect_breakout(
        cls,
        df: pd.DataFrame,
        ma_period: int = 100,
        pre_breakout_window: Optional[int] = None,
    ) -> Dict[str, Any]:
        if df is None or len(df) < ma_period:
            return cls._empty_breakout_result()

        pre_window = (
            pre_breakout_window
            if pre_breakout_window is not None
            else cls.DEFAULT_PRE_BREAKOUT_WINDOW
        )

        ma = df["close"].rolling(window=ma_period).mean()
        close_vals = df["close"].values
        ma_vals = ma.values
        n = len(df)

        latest_price = float(close_vals[-1])
        latest_ma = float(ma_vals[-1])

        if np.isnan(latest_ma):
            return cls._empty_breakout_result()

        is_breakout = latest_price > latest_ma

        # Legacy: consecutive bars above MA (latest-backward run length)
        breakout_days = 0
        for i in range(n - 1, -1, -1):
            if np.isnan(ma_vals[i]) or close_vals[i] <= ma_vals[i]:
                break
            breakout_days += 1

        # New: locate most recent real upward crossing
        breakout_bar_index: Optional[int] = None
        for i in range(n - 1, 0, -1):
            if np.isnan(ma_vals[i]) or np.isnan(ma_vals[i - 1]):
                continue
            if close_vals[i - 1] <= ma_vals[i - 1] and close_vals[i] > ma_vals[i]:
                breakout_bar_index = i
                break

        bars_since_breakout: Optional[int] = (
            (n - 1 - breakout_bar_index) if breakout_bar_index is not None else None
        )

        pre_breakout_below_ratio = 0.0
        pre_breakout_consecutive_below_bars = 0
        if breakout_bar_index is not None and breakout_bar_index >= 1:
            start = max(0, breakout_bar_index - pre_window)
            pre_closes = close_vals[start:breakout_bar_index]
            pre_mas = ma_vals[start:breakout_bar_index]
            valid_mask = ~np.isnan(pre_mas)
            total = int(valid_mask.sum())
            if total > 0:
                below_count = int((pre_closes[valid_mask] <= pre_mas[valid_mask]).sum())
                pre_breakout_below_ratio = below_count / total

            # Count consecutive below-MA bars immediately before the crossing
            for j in range(breakout_bar_index - 1, -1, -1):
                if np.isnan(ma_vals[j]):
                    break
                if close_vals[j] <= ma_vals[j]:
                    pre_breakout_consecutive_below_bars += 1
                else:
                    break

        return {
            "is_breakout": is_breakout,
            "breakout_days": breakout_days,
            "ma_value": latest_ma,
            "price": latest_price,
            "distance_pct": (
                (latest_price - latest_ma) / latest_ma * 100 if latest_ma > 0 else 0
            ),
            "breakout_bar_index": breakout_bar_index,
            "bars_since_breakout": bars_since_breakout,
            "pre_breakout_below_ratio": round(pre_breakout_below_ratio, 4),
            "pre_breakout_consecutive_below_bars": pre_breakout_consecutive_below_bars,
        }

    @classmethod
    def detect_pullback_support(
        cls, df: pd.DataFrame, ma_period: int = 20, tolerance: float = None
    ) -> Dict[str, Any]:
        """Detect if price has pulled back to MA and found support."""
        tol = tolerance if tolerance is not None else cls.SUPPORT_TOLERANCE
        if df is None or len(df) < ma_period:
            return {"is_pullback_support": False, "ma_value": 0.0}

        ma = df["close"].rolling(window=ma_period).mean()
        latest_price = float(df["close"].iloc[-1])
        latest_ma = float(ma.iloc[-1])
        latest_low = float(df["low"].iloc[-1])

        if np.isnan(latest_ma) or latest_ma <= 0:
            return {"is_pullback_support": False, "ma_value": 0.0}

        distance = (latest_price - latest_ma) / latest_ma
        low_distance = (latest_low - latest_ma) / latest_ma

        is_pullback = (
            latest_price >= latest_ma
            and abs(distance) <= tol
            and low_distance <= tol
        )

        return {
            "is_pullback_support": is_pullback,
            "ma_value": latest_ma,
            "price": latest_price,
            "distance_pct": distance * 100,
        }
