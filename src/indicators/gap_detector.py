# -*- coding: utf-8 -*-
"""
Gap Detector — identifies price gaps and their sub-semantics.

This module distinguishes four book-level gap semantics that used to be
collapsed into a single ``is_breakaway`` boolean:

- **Breakaway gap** (strict): gap-up that additionally broke a key level
  (descending trendline / prior range ceiling / compressed platform box)
  with volume confirmation and no exhaustion risk.
- **Continuation gap**: gap-up in an established uptrend after a recent
  breakaway event, with lighter volume confirmation.
- **Exhaustion gap risk**: high-position gap-up flagged by multiple
  heuristics (cumulative rally, consecutive big-yang, MA distance, volume
  acceleration).  Acts as a veto, not a buy point.
- **Gap retest hold**: within N bars after a recent gap-up, price retested
  the gap's upper edge without filling it, with volume shrinking.

Backward compatibility: the legacy ``is_breakaway`` / ``is_gap_up`` /
``is_exhaustion_risk`` keys keep their original semantics and thresholds.
All newly introduced fields are additive and default to False / 0 / "" so
that existing consumers (``leader_score_calculator``,
``hot_theme_factor_enricher``, ``extreme_strength_scorer`` …) are not
affected.
"""

from typing import Any, Dict, List

import numpy as np
import pandas as pd


class GapDetector:
    """Stateless detector for price gaps."""

    # Legacy thresholds — keep unchanged for backward compatibility.
    VOLUME_RATIO_THRESHOLD = 1.5
    LEGACY_EXHAUSTION_RALLY_PCT = 0.30

    # Upgraded exhaustion risk thresholds.
    EXHAUSTION_BIG_YANG_BODY_PCT = 0.03
    EXHAUSTION_CONSECUTIVE_BIG_YANG_MIN = 3
    EXHAUSTION_MA20_DISTANCE_PCT = 0.12
    EXHAUSTION_MA100_DISTANCE_PCT = 0.25
    EXHAUSTION_VOL_TODAY_RATIO = 3.0
    EXHAUSTION_VOL_YEST_RATIO = 2.0

    # Continuation gap thresholds.
    CONTINUATION_VOLUME_RATIO_MIN = 1.2

    # Platform box: range / mid-price threshold to qualify as compressed.
    PLATFORM_RANGE_RATIO_MAX = 0.12

    @classmethod
    def detect_gaps(cls, df: pd.DataFrame, lookback: int = 20) -> List[Dict[str, Any]]:
        if df is None or len(df) < 2:
            return []

        gaps = []
        start = max(0, len(df) - lookback)
        for i in range(max(start, 1), len(df)):
            prev_high = float(df["high"].iloc[i - 1])
            prev_low = float(df["low"].iloc[i - 1])
            curr_high = float(df["high"].iloc[i])
            curr_low = float(df["low"].iloc[i])

            if curr_low > prev_high:
                gaps.append({
                    "index": i,
                    "date": df["date"].iloc[i] if "date" in df.columns else i,
                    "direction": "up",
                    "gap_low": prev_high,
                    "gap_high": curr_low,
                    "gap_pct": (curr_low - prev_high) / prev_high * 100,
                })
            elif curr_high < prev_low:
                gaps.append({
                    "index": i,
                    "date": df["date"].iloc[i] if "date" in df.columns else i,
                    "direction": "down",
                    "gap_low": curr_high,
                    "gap_high": prev_low,
                    "gap_pct": (curr_high - prev_low) / prev_low * 100,
                })

        return gaps

    # ─────────────────────────────────────────────────────────────
    # Key-level detection
    # ─────────────────────────────────────────────────────────────
    @classmethod
    def _detect_broken_key_level(
        cls, df: pd.DataFrame, lookback: int = 20
    ) -> Dict[str, Any]:
        """Return the most relevant key level broken by the latest close.

        Priority:
            1. Descending trendline (TrendlineDetector downtrend)
            2. Prior N-day range ceiling — classified as ``platform_box``
               when the prior window's high-low range is compressed.

        Returns a dict with ``broke_key_level`` / ``key_level_type`` /
        ``key_level_price``.  ``key_level_type`` is one of
        ``trendline | range_ceiling | platform_box | none``.
        """
        none_result = {
            "broke_key_level": False,
            "key_level_type": "none",
            "key_level_price": 0.0,
        }
        if df is None or len(df) < 3:
            return none_result

        curr_close = float(df["close"].iloc[-1])

        # 1. Descending trendline break (uses existing TrendlineDetector).
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
                    "broke_key_level": True,
                    "key_level_type": "trendline",
                    "key_level_price": round(line_val, 4),
                }
        except Exception:
            # Detector errors are non-fatal: fall through to range logic.
            pass

        # 2. Prior range ceiling / platform box.
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
            "broke_key_level": True,
            "key_level_type": "platform_box" if is_platform else "range_ceiling",
            "key_level_price": round(prior_high, 4),
        }

    # ─────────────────────────────────────────────────────────────
    # Exhaustion risk
    # ─────────────────────────────────────────────────────────────
    @classmethod
    def _assess_exhaustion_risk(cls, df: pd.DataFrame) -> Dict[str, Any]:
        """Multi-factor exhaustion risk assessment.

        Level is ``high`` when ≥ 2 heuristics hit, ``warn`` when exactly 1
        hits, ``none`` otherwise.  The legacy ``20d_rally>30%`` heuristic
        is preserved so that ``is_exhaustion_risk`` (legacy semantics)
        remains stable for existing snapshots.
        """
        reasons: List[str] = []
        if df is None or "close" not in df.columns or len(df) < 21:
            return {"risk": False, "level": "none", "reasons": reasons, "legacy_risk": False}

        close_col = df["close"]
        close_now = float(close_col.iloc[-1])
        legacy_risk = False

        close_20d_ago = float(close_col.iloc[-21])
        if close_20d_ago > 0:
            rally_pct = (close_now - close_20d_ago) / close_20d_ago
            if rally_pct > cls.LEGACY_EXHAUSTION_RALLY_PCT:
                legacy_risk = True
                reasons.append("20d_rally>30%")

        # Consecutive big-yang within last 5 bars (close > open, body ≥ 3%).
        if len(df) >= 6 and "open" in df.columns:
            big_yang = 0
            for i in range(-5, 0):
                o = float(df["open"].iloc[i])
                c = float(close_col.iloc[i])
                if o > 0 and c > o and (c - o) / o >= cls.EXHAUSTION_BIG_YANG_BODY_PCT:
                    big_yang += 1
            if big_yang >= cls.EXHAUSTION_CONSECUTIVE_BIG_YANG_MIN:
                reasons.append(f"consecutive_big_yang={big_yang}")

        # MA20 distance.
        if len(df) >= 20:
            ma20 = float(close_col.iloc[-20:].mean())
            if ma20 > 0 and (close_now - ma20) / ma20 > cls.EXHAUSTION_MA20_DISTANCE_PCT:
                reasons.append("ma20_distance>12%")

        # MA100 distance.
        if len(df) >= 100:
            ma100 = float(close_col.iloc[-100:].mean())
            if ma100 > 0 and (close_now - ma100) / ma100 > cls.EXHAUSTION_MA100_DISTANCE_PCT:
                reasons.append("ma100_distance>25%")

        # Volume acceleration: today ≥3x and yesterday ≥2x a 5-bar average
        # ending at t-3 (excludes the two latest bars from the baseline).
        if len(df) >= 7 and "volume" in df.columns:
            vol_today = float(df["volume"].iloc[-1])
            vol_yest = float(df["volume"].iloc[-2])
            baseline = df["volume"].iloc[-7:-2]
            avg5 = float(baseline.mean()) if not baseline.empty else 0.0
            if (
                avg5 > 0
                and vol_today / avg5 >= cls.EXHAUSTION_VOL_TODAY_RATIO
                and vol_yest / avg5 >= cls.EXHAUSTION_VOL_YEST_RATIO
            ):
                reasons.append("volume_acceleration")

        if len(reasons) >= 2:
            level = "high"
        elif len(reasons) == 1:
            level = "warn"
        else:
            level = "none"

        return {
            "risk": level != "none",
            "level": level,
            "reasons": reasons,
            "legacy_risk": legacy_risk,
        }

    # ─────────────────────────────────────────────────────────────
    # Breakaway gap (legacy + strict)
    # ─────────────────────────────────────────────────────────────
    @classmethod
    def detect_breakaway_gap(cls, df: pd.DataFrame) -> Dict[str, Any]:
        """Detect breakaway gap on the latest bar.

        Returns a dict that preserves the legacy fields
        (``is_breakaway``, ``is_gap_up``, ``is_exhaustion_risk``,
        ``volume_ratio``, ``gap_pct``) and adds the new sub-semantic
        fields described in the module docstring.
        """
        empty = {
            "is_breakaway": False,
            "is_breakaway_strict": False,
            "is_gap_up": False,
            "is_exhaustion_risk": False,
            "exhaustion_risk_level": "none",
            "exhaustion_risk_reasons": [],
            "volume_ratio": 0.0,
            "gap_pct": 0.0,
            "gap_low": 0.0,
            "gap_high": 0.0,
            "broke_key_level": False,
            "key_level_type": "none",
            "key_level_price": 0.0,
        }
        if df is None or len(df) < 6:
            return empty

        prev_high = float(df["high"].iloc[-2])
        curr_low = float(df["low"].iloc[-1])
        is_gap_up = curr_low > prev_high

        vol_avg = (
            float(df["volume"].iloc[-6:-1].mean())
            if "volume" in df.columns
            else 0.0
        )
        curr_vol = float(df["volume"].iloc[-1]) if "volume" in df.columns else 0.0
        vol_ratio = curr_vol / vol_avg if vol_avg > 0 else 0.0

        exh = (
            cls._assess_exhaustion_risk(df)
            if is_gap_up
            else {"risk": False, "level": "none", "reasons": [], "legacy_risk": False}
        )

        # Legacy ``is_breakaway`` uses only the 20d-rally exhaustion rule so
        # downstream consumers (leader score, hot theme enricher, extreme
        # strength scorer) see stable behaviour.
        is_breakaway_legacy = (
            is_gap_up
            and vol_ratio >= cls.VOLUME_RATIO_THRESHOLD
            and not exh.get("legacy_risk", False)
        )

        key_level = (
            cls._detect_broken_key_level(df)
            if is_gap_up
            else {
                "broke_key_level": False,
                "key_level_type": "none",
                "key_level_price": 0.0,
            }
        )

        is_breakaway_strict = (
            is_gap_up
            and vol_ratio >= cls.VOLUME_RATIO_THRESHOLD
            and key_level["broke_key_level"]
            and not exh.get("risk", False)
        )

        gap_pct = (
            (curr_low - prev_high) / prev_high * 100
            if is_gap_up and prev_high > 0
            else 0.0
        )

        return {
            "is_breakaway": is_breakaway_legacy,
            "is_breakaway_strict": is_breakaway_strict,
            "is_gap_up": is_gap_up,
            # Legacy ``is_exhaustion_risk`` keeps the 20d-rally semantics.
            "is_exhaustion_risk": exh.get("legacy_risk", False),
            "exhaustion_risk_level": exh.get("level", "none"),
            "exhaustion_risk_reasons": list(exh.get("reasons", [])),
            "volume_ratio": vol_ratio,
            "gap_pct": gap_pct,
            "gap_low": round(prev_high, 4) if is_gap_up else 0.0,
            "gap_high": round(curr_low, 4) if is_gap_up else 0.0,
            "broke_key_level": key_level["broke_key_level"],
            "key_level_type": key_level["key_level_type"],
            "key_level_price": key_level["key_level_price"],
        }

    # ─────────────────────────────────────────────────────────────
    # Continuation gap
    # ─────────────────────────────────────────────────────────────
    @classmethod
    def detect_continuation_gap(cls, df: pd.DataFrame) -> Dict[str, Any]:
        """Detect a continuation gap on the latest bar.

        A continuation gap requires: gap-up + modest volume confirmation +
        established uptrend (close > MA20 > MA60 with MA20 rising) + no
        exhaustion risk.  Returns at minimum ``is_continuation``.
        """
        empty = {
            "is_continuation": False,
            "gap_pct": 0.0,
            "volume_ratio": 0.0,
        }
        if df is None or "close" not in df.columns or len(df) < 60:
            return empty

        prev_high = float(df["high"].iloc[-2])
        curr_low = float(df["low"].iloc[-1])
        is_gap_up = curr_low > prev_high
        if not is_gap_up:
            return empty

        if "volume" not in df.columns:
            return empty
        vol_avg = float(df["volume"].iloc[-6:-1].mean())
        vol_now = float(df["volume"].iloc[-1])
        vol_ratio = vol_now / vol_avg if vol_avg > 0 else 0.0
        if vol_ratio < cls.CONTINUATION_VOLUME_RATIO_MIN:
            return {**empty, "volume_ratio": vol_ratio}

        close_col = df["close"]
        ma20 = float(close_col.iloc[-20:].mean())
        ma60 = float(close_col.iloc[-60:].mean())
        ma20_prev = float(close_col.iloc[-25:-5].mean())
        close_now = float(close_col.iloc[-1])
        uptrend = close_now > ma20 > ma60 and ma20 > ma20_prev
        if not uptrend:
            return {**empty, "volume_ratio": vol_ratio}

        exh = cls._assess_exhaustion_risk(df)
        if exh.get("risk"):
            return {**empty, "volume_ratio": vol_ratio}

        gap_pct = (curr_low - prev_high) / prev_high * 100 if prev_high > 0 else 0.0
        return {
            "is_continuation": True,
            "gap_pct": gap_pct,
            "volume_ratio": vol_ratio,
        }

    # ─────────────────────────────────────────────────────────────
    # Gap retest hold (second-entry buy point)
    # ─────────────────────────────────────────────────────────────
    @classmethod
    def detect_gap_retest_hold(
        cls,
        df: pd.DataFrame,
        lookback: int = 6,
        tolerance_pct: float = 0.01,
    ) -> Dict[str, Any]:
        """Detect a retest-hold on a recent gap-up event.

        Scans the last ``lookback`` bars (excluding the current bar) for a
        gap-up; if found, checks whether the current bar retraced into the
        gap's upper edge without filling it, held above the gap's lower
        edge, printed a non-bearish close, and did so on reduced volume.
        """
        empty = {
            "retest_hold": False,
            "bars_since_event": -1,
            "gap_low": 0.0,
            "gap_high": 0.0,
            "touched_upper_edge": False,
            "held_above_gap_low": False,
            "volume_shrinking": False,
        }
        if df is None or len(df) < lookback + 2:
            return empty

        n = len(df)
        event_idx = -1
        gap_low = 0.0
        gap_high = 0.0
        # Anchor retest detection to a **breakaway-grade** gap-up only.
        # A bare ``curr_low > prev_high`` noise gap in a random-walk series
        # would otherwise produce false positives; require the historical
        # bar to also show a genuine volume surge (≥ VOLUME_RATIO_THRESHOLD)
        # before treating it as a second-entry anchor.
        has_volume = "volume" in df.columns
        start = max(1, n - lookback - 1)
        for i in range(n - 2, start - 1, -1):
            ph = float(df["high"].iloc[i - 1])
            cl = float(df["low"].iloc[i])
            if ph <= 0 or cl <= ph:
                continue
            if has_volume and i >= 5:
                baseline = df["volume"].iloc[max(0, i - 5) : i]
                avg = float(baseline.mean()) if not baseline.empty else 0.0
                vol_here = float(df["volume"].iloc[i])
                if avg <= 0 or vol_here / avg < cls.VOLUME_RATIO_THRESHOLD:
                    continue
            else:
                # Without sufficient volume history we cannot grade the
                # event; refuse to emit a retest signal rather than guess.
                continue
            event_idx = i
            gap_low = ph
            gap_high = cl
            break

        if event_idx == -1:
            return empty

        curr_low = float(df["low"].iloc[-1])
        curr_close = float(df["close"].iloc[-1])
        curr_open = (
            float(df["open"].iloc[-1]) if "open" in df.columns else curr_close
        )

        touched_upper = (
            curr_low <= gap_high * (1 + tolerance_pct)
            and curr_low >= gap_low * (1 - tolerance_pct)
        )
        held = curr_close > gap_low

        shrinking = False
        if "volume" in df.columns:
            vol_now = float(df["volume"].iloc[-1])
            avg5 = float(df["volume"].iloc[-6:-1].mean())
            shrinking = avg5 > 0 and vol_now / avg5 <= 0.9

        bullish_close = curr_close >= curr_open
        bars_since_event = (n - 1) - event_idx
        retest_hold = touched_upper and held and shrinking and bullish_close

        return {
            "retest_hold": bool(retest_hold),
            "bars_since_event": bars_since_event,
            "gap_low": round(gap_low, 4),
            "gap_high": round(gap_high, 4),
            "touched_upper_edge": bool(touched_upper),
            "held_above_gap_low": bool(held),
            "volume_shrinking": bool(shrinking),
        }

    # ─────────────────────────────────────────────────────────────
    # Event locator — ``bars_since_breakaway_gap``
    # ─────────────────────────────────────────────────────────────
    @classmethod
    def locate_recent_breakaway_gap(
        cls, df: pd.DataFrame, lookback: int = 10
    ) -> Dict[str, Any]:
        """Find the most recent breakaway-grade gap-up within ``lookback``.

        Returns the 0-based bar index, ``bars_since_event`` (``-1`` if no
        event found within the window), and the gap's lower edge price.  A
        gap-up qualifies as a breakaway-grade event when the intraday
        volume ratio vs the prior 5-bar average reaches
        ``VOLUME_RATIO_THRESHOLD``.
        """
        empty = {
            "event_bar_index": -1,
            "bars_since_event": -1,
            "gap_low": 0.0,
            "gap_high": 0.0,
        }
        if df is None or len(df) < 7 or "volume" not in df.columns:
            return empty

        n = len(df)
        start = max(6, n - lookback)
        for i in range(n - 1, start - 1, -1):
            ph = float(df["high"].iloc[i - 1])
            cl = float(df["low"].iloc[i])
            if ph <= 0 or cl <= ph:
                continue
            baseline = df["volume"].iloc[max(0, i - 5) : i]
            if baseline.empty:
                continue
            avg = float(baseline.mean())
            if avg <= 0:
                continue
            if float(df["volume"].iloc[i]) / avg >= cls.VOLUME_RATIO_THRESHOLD:
                return {
                    "event_bar_index": i,
                    "bars_since_event": (n - 1) - i,
                    "gap_low": round(ph, 4),
                    "gap_high": round(cl, 4),
                }
        return empty


# Suppress unused-import warning for numpy; retained for parity with other
# detector modules that expose numpy-backed helpers in their public API.
_ = np
