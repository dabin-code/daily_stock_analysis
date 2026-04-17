# -*- coding: utf-8 -*-
"""
Entry Strategies A, B, C, D, D-Enhanced, and E.

Strategy A: Trendline Breakout
  - Detect downtrend resistance line
  - Price breaks above the trendline
  - Volume confirmation

Strategy B: 123 Bottom Reversal
  - 123 bottom pattern detected
  - Breakout above Point 2 confirmed

Strategy C: Gap/Limit-Up Breakout
  - Breakaway gap (gap-up + volume surge) OR
  - Limit-up that breaks above the recent high
  - Volume confirmation (> 1.5x 5-day average)

Strategy D: MA100 Selection (daily, no 60-min yet)
  Step 1: Price must be above MA100
  Step 2: MA100/MA20 breakout confirmed
  Step 3: Pullback to MA support (optional entry refinement)
  Output: stop-loss price based on the highest MA below price

Strategy D-Enhanced: MA100 Selection with 60-min precise entry
  Extends Strategy D with multi-timeframe alignment and MACD divergence
  from 60-minute bars for more precise entry timing.
"""

from typing import Dict, Any, Optional

import numpy as np
import pandas as pd

from src.indicators.ma_breakout_detector import MABreakoutDetector
from src.indicators.gap_detector import GapDetector
from src.indicators.limit_up_detector import LimitUpDetector
from src.indicators.trendline_detector import TrendlineDetector
from src.indicators.pattern_detector import PatternDetector
from src.indicators.multi_timeframe_analyzer import MultiTimeframeAnalyzer
from src.indicators.low_123_trendline_detector import Low123TrendlineDetector
from src.indicators.bottom_divergence_breakout_detector import BottomDivergenceBreakoutDetector


class EntryStrategyC:
    """Gap/Limit-Up Breakout strategy — structured sub-semantic output.

    The legacy ``triggered`` / ``score`` / ``reason`` / ``gap`` / ``limit_up``
    keys are preserved so that existing callers
    (five-layer pipeline, leader score, bot commands …) continue to work.
    New structured keys expose the concrete buy-point classification so
    downstream services (setup_resolver, entry_maturity_assessor, report
    renderer) can route stop-loss and commentary consistently:

    - ``sub_setup_type``: one of ``breakaway_gap_strict``, ``continuation_gap``,
      ``limitup_structure``, ``retest_second_entry``, ``none``
    - ``entry_price`` / ``support_price``
    - ``stop_loss_price`` / ``stop_loss_basis``

    Priority (highest first) when multiple sub-semantics co-exist on the
    same bar: retest_second_entry > limitup_structure >
    breakaway_gap_strict > continuation_gap.
    """

    _STOP_LOSS_BUFFER = 0.99  # 1% cushion below retest support
    _FALLBACK_STOP_PCT = 0.97  # 3% stop when no structural anchor is known

    @classmethod
    def evaluate(cls, df: pd.DataFrame, board: str = "main") -> Dict[str, Any]:
        empty: Dict[str, Any] = {
            "triggered": False,
            "sub_setup_type": "none",
            "entry_price": None,
            "stop_loss_price": None,
            "stop_loss_basis": "",
            "support_price": 0.0,
            "reason": "insufficient data",
            "score": 0,
        }
        if df is None or len(df) < 10:
            return empty

        gap_result = GapDetector.detect_breakaway_gap(df)
        cont_result = GapDetector.detect_continuation_gap(df)
        gap_retest = GapDetector.detect_gap_retest_hold(df)
        limit_result = LimitUpDetector.is_breakout_limit_up(df, board=board)
        limit_retest = LimitUpDetector.detect_limitup_retest_hold(df, board=board)

        latest = df.iloc[-1]
        close_px = float(latest["close"])
        open_px = float(latest.get("open", close_px))

        sub_setup = "none"
        entry_price: Optional[float] = None
        stop_loss_price: Optional[float] = None
        stop_loss_basis = ""
        support_price = 0.0
        reasons: list[str] = []

        # ── Priority 1: retest second entry (gap or limit-up) ─────────
        if gap_retest.get("retest_hold") or limit_retest.get("retest_hold"):
            sub_setup = "retest_second_entry"
            entry_price = close_px
            if gap_retest.get("retest_hold"):
                support_price = float(gap_retest.get("gap_high", 0.0))
                gap_low = float(gap_retest.get("gap_low", 0.0))
                stop_loss_price = gap_low if gap_low > 0 else support_price * cls._STOP_LOSS_BUFFER
                stop_loss_basis = "retest_gap_low" if gap_low > 0 else "retest_support_buffer"
                reasons.append("retest-hold on breakaway gap")
            else:
                support_price = float(limit_retest.get("support_price", 0.0))
                stop_loss_price = (
                    support_price * cls._STOP_LOSS_BUFFER
                    if support_price > 0
                    else close_px * cls._FALLBACK_STOP_PCT
                )
                stop_loss_basis = "retest_support_buffer"
                reasons.append("retest-hold on limit-up support")

        # ── Priority 2: structural limit-up breakout ─────────────────
        elif limit_result.get("is_structural_breakout"):
            sub_setup = "limitup_structure"
            entry_price = close_px
            key_px = float(limit_result.get("key_level_price", 0.0))
            # Stop-loss = min(open-of-day, key-level) to protect against
            # an open-high-close-low failure on the limit-up day.
            candidates = [p for p in (open_px, key_px) if p > 0]
            stop_loss_price = min(candidates) if candidates else close_px * cls._FALLBACK_STOP_PCT
            stop_loss_basis = "limitup_day_open"
            support_price = key_px
            reasons.append(
                "limit-up structural breakout"
                + (f" ({limit_result.get('key_level_type', '')})" if key_px > 0 else "")
            )

        # ── Priority 3: strict breakaway gap ─────────────────────────
        elif gap_result.get("is_breakaway_strict"):
            sub_setup = "breakaway_gap_strict"
            entry_price = close_px
            gap_low = float(gap_result.get("gap_low", 0.0))
            stop_loss_price = gap_low if gap_low > 0 else close_px * cls._FALLBACK_STOP_PCT
            stop_loss_basis = "gap_low"
            support_price = float(gap_result.get("gap_high", 0.0))
            reasons.append(
                f"breakaway gap (+{gap_result.get('gap_pct', 0):.1f}%) broke "
                f"{gap_result.get('key_level_type', 'key level')}"
            )

        # ── Priority 4: continuation gap inside an established trend ──
        elif cont_result.get("is_continuation_gap"):
            sub_setup = "continuation_gap"
            entry_price = close_px
            gap_low = float(cont_result.get("gap_low", 0.0))
            stop_loss_price = gap_low if gap_low > 0 else close_px * cls._FALLBACK_STOP_PCT
            stop_loss_basis = "continuation_gap_low"
            support_price = float(cont_result.get("gap_high", 0.0))
            reasons.append(
                f"continuation gap (+{cont_result.get('gap_pct', 0):.1f}%) in uptrend"
            )

        triggered = sub_setup != "none"

        # ── Scoring ────────────────────────────────────────────────
        vol_avg = float(df["volume"].iloc[-6:-1].mean()) if len(df) >= 6 else 0.0
        vol_curr = float(df["volume"].iloc[-1])
        vol_ratio = vol_curr / vol_avg if vol_avg > 0 else 0.0

        score = 0
        if sub_setup == "retest_second_entry":
            score = 65
            # Retest quality favours shrinking volume; reward contraction.
            if vol_ratio and vol_ratio < 0.8:
                score = min(100, score + 15)
            elif vol_ratio and vol_ratio < 1.0:
                score = min(100, score + 8)
        elif sub_setup == "limitup_structure":
            score = 60
        elif sub_setup == "breakaway_gap_strict":
            score = 55
        elif sub_setup == "continuation_gap":
            score = 45

        # Volume surge bonus for breakout-style setups only.
        if sub_setup in ("limitup_structure", "breakaway_gap_strict", "continuation_gap"):
            if vol_ratio >= 2.0:
                score = min(100, score + 20)
            elif vol_ratio >= 1.5:
                score = min(100, score + 10)

        return {
            # ── Legacy keys (preserved) ──
            "triggered": triggered,
            "reason": " + ".join(reasons) if reasons else "no signal",
            "score": score,
            "gap": gap_result,
            "limit_up": limit_result,
            "volume_ratio": vol_ratio,
            # ── New structured keys ──
            "sub_setup_type": sub_setup,
            "entry_price": entry_price,
            "stop_loss_price": stop_loss_price,
            "stop_loss_basis": stop_loss_basis,
            "support_price": support_price,
            "continuation_gap": cont_result,
            "gap_retest": gap_retest,
            "limit_retest": limit_retest,
        }


class EntryStrategyD:
    """MA100 Selection strategy (daily version)."""

    @classmethod
    def evaluate(
        cls,
        df: pd.DataFrame,
        ma100_confirm_days: int = 3,
        support_tolerance: float = 0.02,
    ) -> Dict[str, Any]:
        if df is None or len(df) < 100:
            return {
                "triggered": False,
                "above_ma100": False,
                "ma20_breakout": False,
                "ma100_breakout_days": 0,
                "pullback_support": False,
                "stop_loss_price": 0.0,
                "stop_loss_ma": "",
                "score": 0,
                "reason": "insufficient data",
            }

        # Step 1: MA100 filter
        ma100_result = MABreakoutDetector.detect_breakout(df, ma_period=100)
        above_ma100 = ma100_result["is_breakout"]
        breakout_days = ma100_result["breakout_days"]

        if not above_ma100:
            return {
                "triggered": False,
                "above_ma100": False,
                "ma20_breakout": False,
                "ma100_breakout_days": 0,
                "pullback_support": False,
                "stop_loss_price": 0.0,
                "stop_loss_ma": "",
                "score": 0,
                "reason": "price below MA100",
            }

        # Step 2: MA20 breakout
        ma20_result = MABreakoutDetector.detect_breakout(df, ma_period=20)
        ma20_breakout = ma20_result["is_breakout"]

        # Step 3: Pullback support (MA20 or MA100)
        pullback_ma20 = MABreakoutDetector.detect_pullback_support(
            df, ma_period=20, tolerance=support_tolerance
        )
        pullback_ma100 = MABreakoutDetector.detect_pullback_support(
            df, ma_period=100, tolerance=support_tolerance
        )

        has_pullback = (
            pullback_ma20["is_pullback_support"]
            or pullback_ma100["is_pullback_support"]
        )

        # Trigger logic: above MA100 + confirmed + (MA20 breakout or pullback support)
        confirmed = breakout_days >= ma100_confirm_days
        triggered = confirmed and (ma20_breakout or has_pullback)

        # Stop-loss: use the highest MA below price as stop reference
        price = float(df["close"].iloc[-1])
        ma20_val = ma20_result.get("ma_value", 0)
        ma100_val = ma100_result.get("ma_value", 0)

        stop_loss_price = 0.0
        stop_loss_ma = ""
        if ma20_val > 0 and ma20_val < price:
            stop_loss_price = ma20_val
            stop_loss_ma = "MA20"
        if ma100_val > 0 and ma100_val < price and ma100_val > stop_loss_price:
            stop_loss_price = ma100_val
            stop_loss_ma = "MA100"
        # Use a tighter stop if MA20 is closer to price
        if ma20_val > 0 and ma20_val < price and ma20_val < ma100_val:
            stop_loss_price = ma20_val
            stop_loss_ma = "MA20"

        # Scoring
        score = 0
        if above_ma100:
            score += 30
        if confirmed:
            score += 20
        if ma20_breakout:
            score += 20
        if has_pullback:
            score += 20
        # Volume bonus
        if len(df) >= 6:
            vol_avg = float(df["volume"].iloc[-6:-1].mean())
            vol_curr = float(df["volume"].iloc[-1])
            vol_ratio = vol_curr / vol_avg if vol_avg > 0 else 0
            if vol_ratio >= 1.5:
                score = min(100, score + 10)

        return {
            "triggered": triggered,
            "above_ma100": above_ma100,
            "ma100_breakout_days": breakout_days,
            "ma20_breakout": ma20_breakout,
            "pullback_support": has_pullback,
            "stop_loss_price": stop_loss_price,
            "stop_loss_ma": stop_loss_ma,
            "score": min(100, score),
            "reason": "MA100 selection" if triggered else "conditions not fully met",
        }


class EntryStrategyDEnhanced:
    """
    MA100 Selection with 60-minute precise entry.

    Combines daily EntryStrategyD logic with multi-timeframe alignment
    and 60-min MACD divergence for better entry timing.
    """

    _mtf = MultiTimeframeAnalyzer()

    @classmethod
    def evaluate(
        cls,
        daily_df: pd.DataFrame,
        intraday_df: Optional[pd.DataFrame] = None,
        ma100_confirm_days: int = 3,
        support_tolerance: float = 0.02,
    ) -> Dict[str, Any]:
        base_result = EntryStrategyD.evaluate(
            daily_df, ma100_confirm_days=ma100_confirm_days,
            support_tolerance=support_tolerance,
        )

        mtf_result = cls._mtf.analyze(
            daily_df=daily_df, intraday_df=intraday_df
        )

        alignment = mtf_result["alignment_score"]
        timing = mtf_result["entry_timing"]
        bull_div = mtf_result["intraday_macd_bull_divergence"]
        bear_div = mtf_result["intraday_macd_bear_divergence"]

        score = base_result["score"]
        if alignment >= 80:
            score = min(100, score + 15)
        elif alignment >= 60:
            score = min(100, score + 5)
        elif alignment < 40:
            score = max(0, score - 10)

        if bull_div:
            score = min(100, score + 10)

        triggered = base_result["triggered"]
        if triggered and alignment < 30:
            triggered = False

        # Trendline confirmation
        trendline_confirmed = False
        if len(daily_df) >= 20:
            tl_result = TrendlineDetector.detect_trendline_breakout(daily_df)
            if tl_result.get("breakout") and tl_result.get("direction") == "up":
                trendline_confirmed = True
                score = min(100, score + 10)

        base_result.update({
            "alignment_score": alignment,
            "entry_timing": timing,
            "daily_trend": mtf_result["daily_trend"],
            "intraday_trend": mtf_result["intraday_trend"],
            "intraday_macd_bull_divergence": bull_div,
            "intraday_macd_bear_divergence": bear_div,
            "trendline_confirmed": trendline_confirmed,
            "score": min(100, score),
        })
        if not triggered:
            base_result["triggered"] = False

        return base_result


class EntryStrategyA:
    """Trendline Breakout strategy."""

    @classmethod
    def evaluate(cls, df: pd.DataFrame) -> Dict[str, Any]:
        if df is None or len(df) < 20:
            return {
                "triggered": False,
                "trendline_breakout": False,
                "score": 0,
                "reason": "insufficient data",
            }

        breakout_result = TrendlineDetector.detect_trendline_breakout(df)
        is_breakout = breakout_result.get("breakout", False)
        direction = breakout_result.get("direction", "none")

        triggered = is_breakout and direction == "up"

        score = 0
        reasons = []

        if triggered:
            score += 50
            reasons.append("trendline breakout (up)")

            # Volume confirmation
            if len(df) >= 6 and "volume" in df.columns:
                vol_avg = float(df["volume"].iloc[-6:-1].mean())
                vol_curr = float(df["volume"].iloc[-1])
                vol_ratio = vol_curr / vol_avg if vol_avg > 0 else 0
                if vol_ratio >= 2.0:
                    score += 30
                    reasons.append(f"volume surge ({vol_ratio:.1f}x)")
                elif vol_ratio >= 1.5:
                    score += 15
                    reasons.append(f"volume increase ({vol_ratio:.1f}x)")

            # Downtrend strength bonus: more touches = stronger breakout
            downtrend = breakout_result.get("downtrend")
            if downtrend and downtrend.get("touch_count", 0) >= 3:
                score += 20
                reasons.append(f"strong resistance ({downtrend['touch_count']} touches)")

        return {
            "triggered": triggered,
            "trendline_breakout": is_breakout,
            "direction": direction,
            "score": min(100, score),
            "reason": " + ".join(reasons) if reasons else "no signal",
            "breakout_detail": breakout_result,
        }


class EntryStrategyB:
    """
    Low-position 123 Bottom Reversal with Downtrend Trendline Confirmation.

    Triggered only when the joint detector returns state='breakout_ready':
      - Prior downtrend context verified
      - P1 at low position
      - Close breaks P2 (trendline remains an optional bonus signal)
    """

    @classmethod
    def evaluate(cls, df: pd.DataFrame) -> Dict[str, Any]:
        if df is None or len(df) < 40:
            return {
                "triggered": False,
                "pattern_123": {"found": False},
                "pattern_123_state": "rejected",
                "entry_price": None,
                "stop_loss_price": None,
                "score": 0,
                "reason": "insufficient data",
            }

        joint = Low123TrendlineDetector.detect(df)
        state = joint.get("state", "rejected")
        triggered = state == "breakout_ready"

        score = 0
        reasons: list = []

        if joint.get("found"):
            score += 20
            reasons.append("123 structure detected")
        if joint.get("is_low_level"):
            score += 10
            reasons.append("low-level position confirmed")
        if joint.get("breakout_point2_confirmed"):
            score += 25
            reasons.append("breakout above P2")
        if joint.get("breakout_trendline_confirmed"):
            score += 25
            reasons.append("trendline broken")
        if joint.get("ma_confirmation"):
            score += 10
            reasons.append("MA confirmation")

        # Trendline quality bonus
        dtl = joint.get("downtrend_line") or {}
        if dtl.get("touch_count", 0) >= 3:
            score += 10
            reasons.append(f"trendline {dtl['touch_count']}-touch quality")

        # signal_strength bonus (caps at 100)
        strength_bonus = int(joint.get("signal_strength", 0.0) * 10)
        score = min(100, score + strength_bonus)

        return {
            "triggered": triggered,
            "pattern_123": joint,           # full joint result; backward compat key
            "pattern_123_state": state,
            "entry_price": joint.get("entry_price"),
            "stop_loss_price": joint.get("stop_loss_price"),
            "score": score,
            "reason": " + ".join(reasons) if reasons else f"no signal (state={state})",
        }


class EntryStrategyE:
    """底背离双突破策略 (Bottom Divergence Double Breakout).

    Uses DIF/DEA-based six-pattern bottom divergence detection combined with
    double breakout confirmation (descending trendline + horizontal resistance).
    Only triggers when the detector state reaches "confirmed".
    """

    @classmethod
    def evaluate(cls, df: pd.DataFrame) -> Dict[str, Any]:
        """Evaluate the bottom divergence double breakout strategy.

        Parameters
        ----------
        df : pd.DataFrame
            OHLCV DataFrame with columns: high, low, close, volume.

        Returns
        -------
        Dict[str, Any]
            Result dict with keys: triggered, pattern_123, pattern_123_state,
            entry_price, stop_loss_price, score, reason.
        """
        if len(df) < 60:
            return {
                "triggered": False,
                "pattern_123": {},
                "pattern_123_state": "rejected",
                "entry_price": None,
                "stop_loss_price": None,
                "score": 0,
                "reason": "insufficient data",
            }

        result = BottomDivergenceBreakoutDetector.detect(df)
        state = result.get("state", "rejected")
        triggered = state == "confirmed"

        # --- scoring ---
        score = 0
        reasons: list[str] = []

        if triggered:
            score = 50
            reasons.append("bottom divergence confirmed")

            pattern_label = result.get("pattern_label", "")
            if pattern_label:
                reasons.append(pattern_label)

            if result.get("sync_breakout"):
                score += 15
                reasons.append("sync double breakout")

            strength = result.get("signal_strength", 0.0)
            strength_bonus = int(strength * 20)
            score = min(100, score + strength_bonus)
        elif state == "divergence_only":
            score = 15
            reasons.append("divergence detected but no breakout")
        elif state == "structure_ready":
            score = 25
            reasons.append("structure ready, awaiting breakout")
        elif state == "late_or_weak":
            score = 10
            reasons.append("late or weak signal")

        return {
            "triggered": triggered,
            "pattern_123": result,
            "pattern_123_state": state,
            "entry_price": result.get("entry_price"),
            "stop_loss_price": result.get("stop_loss_price"),
            "score": score,
            "reason": " + ".join(reasons) if reasons else f"no signal (state={state})",
        }
