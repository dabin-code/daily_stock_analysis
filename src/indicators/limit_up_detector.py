# -*- coding: utf-8 -*-
"""
Limit-Up Detector — identifies daily limit-up events and whether they
form a structural breakout, plus high-acceleration risk and retest-hold
second-entry semantics.

Board types:
- main: 10% limit (default, threshold 9.8%)
- star: 20% limit (科创板 688xxx)
- gem_new: 20% limit (创业板 300xxx post-reform)

Backward compatibility: the legacy fields (``is_limit_up``,
``is_breakout_high``, ``pct_chg``, ``prev_high``, ``close``) retain their
original semantics.  All added fields default to False / 0 / "" so the
existing downstream consumers (``leader_score_calculator``,
``hot_theme_factor_enricher``, ``sector_heat_engine`` …) continue to work
unchanged.
"""

from typing import Any, Dict, List

import numpy as np
import pandas as pd


LIMIT_THRESHOLDS = {
    "main": 9.8,
    "star": 19.5,
    "gem_new": 19.5,
}


class LimitUpDetector:
    """Stateless detector for limit-up events and structural breakouts."""

    PLATFORM_RANGE_RATIO_MAX = 0.12
    HIGH_ACCEL_CONSECUTIVE_BOARDS = 3
    # Two-tier exhaustion detection to mirror GapDetector.
    #   fatal:      single hit is enough to mark risk=True
    #   non-fatal:  at least two distinct hits required to raise the flag,
    #               preventing a healthy first-board limit-up with natural
    #               volume expansion from being misclassified as a trap.
    HIGH_ACCEL_FATAL_20D_RALLY_PCT = 0.50
    HIGH_ACCEL_20D_RALLY_PCT = 0.40
    HIGH_ACCEL_VOL_RATIO = 3.0
    HIGH_ACCEL_VOL_PREV_RATIO = 2.0
    HIGH_ACCEL_MA20_DISTANCE_PCT = 0.12

    @classmethod
    def is_limit_up(cls, pct_chg: float, board: str = "main") -> bool:
        threshold = LIMIT_THRESHOLDS.get(board, 9.8)
        return pct_chg >= threshold

    # ─────────────────────────────────────────────────────────────
    # Breakout limit-up (legacy + structural)
    # ─────────────────────────────────────────────────────────────
    @classmethod
    def is_breakout_limit_up(
        cls,
        df: pd.DataFrame,
        lookback: int = 20,
        board: str = "main",
    ) -> Dict[str, Any]:
        """Detect whether the latest bar is a limit-up closing above the
        prior ``lookback``-bar high, and enrich with structural context.

        The returned dict keeps the legacy keys
        (``is_limit_up``, ``is_breakout_high``, ``pct_chg``, ``prev_high``,
        ``close``) unchanged, and adds:

        - ``is_structural_breakout``: bool — limit-up + legacy breakout +
          broke a key level (trendline / range ceiling / platform box)
          and not classified as high-acceleration risk.
        - ``key_level_type`` / ``key_level_price``
        - ``consecutive_limit_up_count`` / ``is_first_board``
        - ``high_acceleration_risk`` / ``high_acceleration_reasons``
        """
        empty = {
            "is_limit_up": False,
            "is_breakout_high": False,
            "is_structural_breakout": False,
            "key_level_type": "none",
            "key_level_price": 0.0,
            "consecutive_limit_up_count": 0,
            "is_first_board": False,
            "high_acceleration_risk": False,
            "high_acceleration_reasons": [],
        }
        if df is None or len(df) < 3:
            return empty

        latest = df.iloc[-1]
        pct = float(latest.get("pct_chg", 0))
        is_lu = cls.is_limit_up(pct, board)

        if not is_lu:
            return {**empty, "pct_chg": pct}

        start = max(0, len(df) - 1 - lookback)
        prev_high = float(df["high"].iloc[start:-1].max())
        curr_close = float(latest["close"])
        is_breakout = curr_close > prev_high

        consecutive = cls._count_consecutive_limit_up(df, board=board)
        is_first = consecutive == 1

        key_level = (
            cls._detect_broken_key_level(df, lookback=lookback)
            if is_breakout
            else {"key_level_type": "none", "key_level_price": 0.0}
        )

        high_accel = cls._assess_high_acceleration_risk(
            df, consecutive_count=consecutive
        )

        is_structural_breakout = (
            is_breakout
            and key_level["key_level_type"] != "none"
            and not high_accel["risk"]
        )

        return {
            "is_limit_up": True,
            "is_breakout_high": is_breakout,
            "is_structural_breakout": is_structural_breakout,
            "pct_chg": pct,
            "prev_high": round(prev_high, 4),
            "close": round(curr_close, 4),
            "key_level_type": key_level["key_level_type"],
            "key_level_price": key_level["key_level_price"],
            "consecutive_limit_up_count": consecutive,
            "is_first_board": is_first,
            "high_acceleration_risk": high_accel["risk"],
            "high_acceleration_reasons": high_accel["reasons"],
        }

    # ─────────────────────────────────────────────────────────────
    # Consecutive board counter
    # ─────────────────────────────────────────────────────────────
    @classmethod
    def _count_consecutive_limit_up(
        cls, df: pd.DataFrame, board: str = "main"
    ) -> int:
        """Count consecutive limit-up bars ending at the latest bar."""
        if df is None or "pct_chg" not in df.columns or df.empty:
            return 0
        count = 0
        for i in range(len(df) - 1, -1, -1):
            pct = float(df["pct_chg"].iloc[i])
            if cls.is_limit_up(pct, board=board):
                count += 1
            else:
                break
        return count

    # ─────────────────────────────────────────────────────────────
    # Key-level detection (shared semantics with GapDetector)
    # ─────────────────────────────────────────────────────────────
    @classmethod
    def _detect_broken_key_level(
        cls, df: pd.DataFrame, lookback: int = 20
    ) -> Dict[str, Any]:
        """Return the most relevant key level broken by the latest close.

        Mirrors the priority used by ``GapDetector._detect_broken_key_level``
        so that both detectors produce comparable ``key_level_type`` values.
        """
        none_result = {"key_level_type": "none", "key_level_price": 0.0}
        if df is None or len(df) < 3:
            return none_result

        curr_close = float(df["close"].iloc[-1])

        try:
            from src.indicators.trendline_detector import TrendlineDetector

            trend = TrendlineDetector.detect_trendline_breakout(df)
            dt = trend.get("downtrend") or {}
            line_val = float(dt.get("line_value_at_last", 0) or 0)
            if (
                trend.get("breakout") is True
                and trend.get("direction") == "up"
                and dt.get("found")
                and line_val > 0
                and curr_close > line_val
            ):
                return {
                    "key_level_type": "trendline",
                    "key_level_price": round(line_val, 4),
                }
        except Exception:
            pass

        start = max(0, len(df) - 1 - lookback)
        prior_highs = df["high"].iloc[start:-1]
        prior_lows = df["low"].iloc[start:-1]
        if prior_highs.empty or prior_lows.empty:
            return none_result

        prior_high = float(prior_highs.max())
        prior_low = float(prior_lows.min())
        if prior_high <= 0 or curr_close <= prior_high:
            return none_result

        prior_mid = (prior_high + prior_low) / 2.0
        is_platform = (
            prior_mid > 0
            and (prior_high - prior_low) / prior_mid < cls.PLATFORM_RANGE_RATIO_MAX
        )
        return {
            "key_level_type": "platform_box" if is_platform else "range_ceiling",
            "key_level_price": round(prior_high, 4),
        }

    # ─────────────────────────────────────────────────────────────
    # High acceleration risk (exhaustion-style filter for limit-ups)
    # ─────────────────────────────────────────────────────────────
    @classmethod
    def _assess_high_acceleration_risk(
        cls, df: pd.DataFrame, consecutive_count: int = 0
    ) -> Dict[str, Any]:
        """Flag the latest limit-up as a potential high-acceleration trap.

        Fatal hits (any single one marks ``risk=True``):
        - ``consecutive_count`` ≥ 3 (3 连板及以上)
        - 20-day cumulative rally > 50% (明显抢筹)

        Non-fatal hits (need at least two to raise the flag):
        - 20-day rally between 40% and 50%
        - Two-day volume acceleration: today ≥ 3.0x of 5-bar avg **and**
          yesterday ≥ 2.0x of that same baseline
        - Distance above MA20 > 12%

        Reasons are prefixed ``fatal:`` or ``combo:`` so downstream logs
        can distinguish decisive vetoes from borderline clusters.
        """
        fatal_reasons: List[str] = []
        non_fatal_reasons: List[str] = []

        if df is None or len(df) < 21:
            return {"risk": False, "reasons": []}

        if consecutive_count >= cls.HIGH_ACCEL_CONSECUTIVE_BOARDS:
            fatal_reasons.append(f"fatal:consecutive_boards={consecutive_count}")

        close_col = df["close"]
        close_now = float(close_col.iloc[-1])
        close_20d_ago = float(close_col.iloc[-21])
        rally_pct = 0.0
        if close_20d_ago > 0:
            rally_pct = (close_now - close_20d_ago) / close_20d_ago
            if rally_pct > cls.HIGH_ACCEL_FATAL_20D_RALLY_PCT:
                fatal_reasons.append("fatal:20d_rally>50%")
            elif rally_pct > cls.HIGH_ACCEL_20D_RALLY_PCT:
                non_fatal_reasons.append("combo:20d_rally>40%")

        if "volume" in df.columns and len(df) >= 7:
            vol_today = float(df["volume"].iloc[-1])
            vol_yest = float(df["volume"].iloc[-2])
            baseline = df["volume"].iloc[-6:-1]
            avg5 = float(baseline.mean()) if not baseline.empty else 0.0
            if (
                avg5 > 0
                and vol_today / avg5 >= cls.HIGH_ACCEL_VOL_RATIO
                and vol_yest / avg5 >= cls.HIGH_ACCEL_VOL_PREV_RATIO
            ):
                non_fatal_reasons.append("combo:vol_acceleration_2d")

        ma20 = float(close_col.iloc[-20:].mean())
        if ma20 > 0 and (close_now - ma20) / ma20 > cls.HIGH_ACCEL_MA20_DISTANCE_PCT:
            non_fatal_reasons.append("combo:ma20_distance>12%")

        risk = bool(fatal_reasons) or len(non_fatal_reasons) >= 2
        return {
            "risk": risk,
            "reasons": [*fatal_reasons, *non_fatal_reasons],
        }

    # ─────────────────────────────────────────────────────────────
    # Limit-up retest-hold (second-entry buy point)
    # ─────────────────────────────────────────────────────────────
    @classmethod
    def detect_limitup_retest_hold(
        cls,
        df: pd.DataFrame,
        lookback: int = 6,
        tolerance_pct: float = 0.01,
        board: str = "main",
    ) -> Dict[str, Any]:
        """Detect a retest-hold after a recent structural limit-up event.

        Scans the last ``lookback`` bars (excluding the current bar) for a
        limit-up bar; if found, checks whether the current bar pulled back
        to the limit-up day's open or the pre-limit-up breakout level,
        held that support, closed non-bearish, and did so on reduced
        volume.
        """
        empty = {
            "retest_hold": False,
            "bars_since_event": -1,
            "support_price": 0.0,
            "touched_support": False,
            "held_above_support": False,
            "volume_shrinking": False,
        }
        if df is None or len(df) < lookback + 2 or "pct_chg" not in df.columns:
            return empty

        n = len(df)
        event_idx = -1
        start = max(1, n - lookback - 1)
        for i in range(n - 2, start - 1, -1):
            pct = float(df["pct_chg"].iloc[i])
            if cls.is_limit_up(pct, board=board):
                event_idx = i
                break

        if event_idx == -1:
            return empty

        # Support price: the pre-limit-up bar's high (breakout level).
        # Falls back to the limit-up bar's open when the prior high is
        # absent.
        support_price = 0.0
        if event_idx - 1 >= 0:
            support_price = float(df["high"].iloc[event_idx - 1])
        if support_price <= 0 and "open" in df.columns:
            support_price = float(df["open"].iloc[event_idx])
        if support_price <= 0:
            return empty

        curr_low = float(df["low"].iloc[-1])
        curr_close = float(df["close"].iloc[-1])
        curr_open = (
            float(df["open"].iloc[-1]) if "open" in df.columns else curr_close
        )

        touched = curr_low <= support_price * (1 + tolerance_pct)
        held = curr_close >= support_price * (1 - tolerance_pct)

        shrinking = False
        if "volume" in df.columns:
            vol_now = float(df["volume"].iloc[-1])
            avg5 = float(df["volume"].iloc[-6:-1].mean())
            shrinking = avg5 > 0 and vol_now / avg5 <= 0.9

        bullish_close = curr_close >= curr_open
        retest_hold = touched and held and shrinking and bullish_close

        return {
            "retest_hold": bool(retest_hold),
            "bars_since_event": (n - 1) - event_idx,
            "support_price": round(support_price, 4),
            "touched_support": bool(touched),
            "held_above_support": bool(held),
            "volume_shrinking": bool(shrinking),
        }

    # ─────────────────────────────────────────────────────────────
    # Event locator — ``bars_since_structural_limitup``
    # ─────────────────────────────────────────────────────────────
    @classmethod
    def locate_recent_structural_limitup(
        cls,
        df: pd.DataFrame,
        lookback: int = 10,
        board: str = "main",
    ) -> Dict[str, Any]:
        """Find the most recent structural-breakout limit-up within window.

        A bar qualifies when it is a limit-up that closed above the prior
        ``lookback``-bar high at that point in time.  Returns the event
        index and ``bars_since_event`` (``-1`` when not found).
        """
        empty = {"event_bar_index": -1, "bars_since_event": -1}
        if df is None or len(df) < lookback + 2 or "pct_chg" not in df.columns:
            return empty

        n = len(df)
        start = max(1, n - lookback)
        for i in range(n - 1, start - 1, -1):
            pct = float(df["pct_chg"].iloc[i])
            if not cls.is_limit_up(pct, board=board):
                continue
            window_start = max(0, i - lookback)
            prior_highs = df["high"].iloc[window_start:i]
            if prior_highs.empty:
                continue
            if float(df["close"].iloc[i]) > float(prior_highs.max()):
                return {"event_bar_index": i, "bars_since_event": (n - 1) - i}
        return empty


# Suppress unused-import warning for numpy; retained for API parity across
# detector modules.
_ = np
