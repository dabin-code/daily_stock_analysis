# -*- coding: utf-8 -*-
"""Shrink Volume Pullback Detector — 缩量回踩支撑企稳检测器。

识别《短线操盘实战技法》中"趋势中缩量回踩 MA5/MA10 支撑并企稳确认"的买点。

State machine outputs:
  rejected     — 前置不满足 / 无有效回踩
  touch_only   — 仅 low 触均线，未收回支撑上方
  stabilizing  — 触碰 + 缩量 + 收盘站回支撑，但未出现阳线企稳信号
  confirmed    — 触碰 + 缩量 + 企稳阳线（收盘站回 + 阳线 or 突破前高）

Support levels:
  MA5  — 高优先级主支撑（对应 YAML "理想买点设在 MA5"）
  MA10 — 次优主支撑（对应 YAML "次优买点设在 MA10"）

设计文档：见 `docs/superpowers/plans/2026-04-16-kline-completeness-governance-implementation-plan.md`
及 `c:\\Users\\iu\\.cursor\\plans\\shrink_pullback复核_50b8b83a.plan.md`。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# 默认参数（可通过 detect() kwargs 覆盖）
# ---------------------------------------------------------------------------
DEFAULT_LOOKBACK = 30              # detector 至少使用的尾部 bar 数
DEFAULT_PULLBACK_WINDOW = 10       # 回踩阶段最多持续的 bar 数
DEFAULT_UPTREND_LOOKBACK = 20      # 回踩前上涨阶段基线窗口
DEFAULT_SHRINK_THRESHOLD = 0.7     # 回踩均量 / 上涨均量 上限
DEFAULT_TOUCH_TOLERANCE_MA5 = 0.01
DEFAULT_TOUCH_TOLERANCE_MA10 = 0.02
DEFAULT_MIN_PULLBACK_DEPTH = 0.01  # 至少 1% 回撤才算回踩
DEFAULT_MAX_PULLBACK_DEPTH = 0.15  # 超过 15% 视为破位，非中继回踩
DEFAULT_MA20_STOP_BUFFER = 0.03    # pullback_low 不得低于 MA20 × (1 - buffer)
DEFAULT_CONFIRM_CLOSE_STRENGTH = 0.5

STATE_REJECTED = "rejected"
STATE_TOUCH_ONLY = "touch_only"
STATE_STABILIZING = "stabilizing"
STATE_CONFIRMED = "confirmed"

SUPPORT_MA5 = "MA5"
SUPPORT_MA10 = "MA10"
SUPPORT_NONE = "none"

MATURITY_HIGH = "high"
MATURITY_MEDIUM = "medium"
MATURITY_LOW = "low"

REJECT_INSUFFICIENT_BARS = "insufficient_bars"
REJECT_NO_UPTREND = "no_uptrend"
REJECT_NO_TOUCH = "no_touch"
REJECT_NO_SHRINK = "no_shrink"
REJECT_SHALLOW_PULLBACK = "shallow_pullback"
REJECT_DEEP_PULLBACK = "deep_pullback"
REJECT_BREAK_MA20 = "break_ma20"


class ShrinkPullbackDetector:
    """Stateless detector for shrink-volume pullback to MA5/MA10 with stabilize confirm."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @classmethod
    def detect(
        cls,
        df: pd.DataFrame,
        *,
        lookback: int = DEFAULT_LOOKBACK,
        pullback_window: int = DEFAULT_PULLBACK_WINDOW,
        uptrend_lookback: int = DEFAULT_UPTREND_LOOKBACK,
        shrink_threshold: float = DEFAULT_SHRINK_THRESHOLD,
        touch_tolerance_ma5: float = DEFAULT_TOUCH_TOLERANCE_MA5,
        touch_tolerance_ma10: float = DEFAULT_TOUCH_TOLERANCE_MA10,
        min_pullback_depth: float = DEFAULT_MIN_PULLBACK_DEPTH,
        max_pullback_depth: float = DEFAULT_MAX_PULLBACK_DEPTH,
        ma20_stop_buffer: float = DEFAULT_MA20_STOP_BUFFER,
        confirm_close_strength: float = DEFAULT_CONFIRM_CLOSE_STRENGTH,
    ) -> Dict[str, Any]:
        """Detect shrink-volume pullback pattern on the latest bar of ``df``.

        ``df`` must contain at least columns: ``open / high / low / close / volume``.
        Rows are expected to be ordered by date ascending, with the last row being
        the latest trading bar (today).
        """
        required_bars = max(lookback, uptrend_lookback + pullback_window, 20)
        if df is None or len(df) < required_bars:
            return cls._rejected_result(REJECT_INSUFFICIENT_BARS)

        df = df.reset_index(drop=True)

        close = df["close"].astype(float)
        open_ = df["open"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float).fillna(0.0)

        ma5 = close.rolling(5).mean()
        ma10 = close.rolling(10).mean()
        ma20 = close.rolling(20).mean()

        latest_idx = len(df) - 1
        latest_close = float(close.iloc[-1])
        latest_open = float(open_.iloc[-1])
        latest_high = float(high.iloc[-1])
        latest_low = float(low.iloc[-1])
        latest_ma5 = float(ma5.iloc[-1]) if pd.notna(ma5.iloc[-1]) else 0.0
        latest_ma10 = float(ma10.iloc[-1]) if pd.notna(ma10.iloc[-1]) else 0.0
        latest_ma20 = float(ma20.iloc[-1]) if pd.notna(ma20.iloc[-1]) else 0.0

        # 1. 趋势前提：多头排列
        if not (latest_ma5 > 0 and latest_ma10 > 0 and latest_ma20 > 0
                and latest_ma5 >= latest_ma10 >= latest_ma20):
            return cls._rejected_result(REJECT_NO_UPTREND)

        # 2. 定位回踩起点 = 最近 pullback_window 内的阶段高点（按 close 计）
        window_start = max(latest_idx - pullback_window + 1, 0)
        window_closes = close.iloc[window_start : latest_idx + 1]
        pullback_start_bar = int(window_closes.idxmax())
        pre_high_close = float(window_closes.max())

        # 3. 回踩阶段最低价
        pullback_slice_low = low.iloc[pullback_start_bar : latest_idx + 1]
        pullback_low = float(pullback_slice_low.min())
        pullback_low_bar = int(pullback_slice_low.idxmin())

        pullback_depth_pct = (
            (pre_high_close - pullback_low) / pre_high_close
            if pre_high_close > 0
            else 0.0
        )
        pullback_days = latest_idx - pullback_start_bar + 1

        if pullback_depth_pct < min_pullback_depth:
            return cls._rejected_result(
                REJECT_SHALLOW_PULLBACK,
                pullback_days=pullback_days,
                pullback_depth_pct=round(pullback_depth_pct, 4),
                pullback_low=round(pullback_low, 4),
            )
        if pullback_depth_pct > max_pullback_depth:
            return cls._rejected_result(
                REJECT_DEEP_PULLBACK,
                pullback_days=pullback_days,
                pullback_depth_pct=round(pullback_depth_pct, 4),
                pullback_low=round(pullback_low, 4),
            )

        # 4. 回踩阶段不应破 MA20（否则已非中继回踩）
        ma20_at_low = float(ma20.iloc[pullback_low_bar]) if pd.notna(ma20.iloc[pullback_low_bar]) else 0.0
        if ma20_at_low > 0 and pullback_low < ma20_at_low * (1.0 - ma20_stop_buffer):
            return cls._rejected_result(
                REJECT_BREAK_MA20,
                pullback_days=pullback_days,
                pullback_depth_pct=round(pullback_depth_pct, 4),
                pullback_low=round(pullback_low, 4),
            )

        # 5. 触碰判定（回踩阶段内任一 bar low 与当日 MA 的距离）
        touched_ma5 = cls._touched_in_window(
            low_series=low, ma_series=ma5,
            start_bar=pullback_start_bar, end_bar=latest_idx,
            tolerance=touch_tolerance_ma5,
        )
        touched_ma10 = cls._touched_in_window(
            low_series=low, ma_series=ma10,
            start_bar=pullback_start_bar, end_bar=latest_idx,
            tolerance=touch_tolerance_ma10,
        )
        if not (touched_ma5 or touched_ma10):
            return cls._rejected_result(
                REJECT_NO_TOUCH,
                pullback_days=pullback_days,
                pullback_depth_pct=round(pullback_depth_pct, 4),
                pullback_low=round(pullback_low, 4),
            )

        # 6. 缩量过程：回踩阶段均量 vs 上涨阶段均量
        # 回踩阶段量能统计排除 latest bar（企稳当日可能放量）以避免干扰
        pullback_vol_slice = volume.iloc[pullback_start_bar : latest_idx]
        uptrend_vol_start = max(pullback_start_bar - uptrend_lookback, 0)
        uptrend_vol_slice = volume.iloc[uptrend_vol_start : pullback_start_bar]

        pullback_avg_vol = float(pullback_vol_slice.mean()) if not pullback_vol_slice.empty else 0.0
        uptrend_avg_vol = float(uptrend_vol_slice.mean()) if not uptrend_vol_slice.empty else 0.0

        volume_shrink_ratio = (
            pullback_avg_vol / uptrend_avg_vol if uptrend_avg_vol > 0 else 0.0
        )
        volume_shrink = (
            uptrend_avg_vol > 0 and 0.0 < volume_shrink_ratio <= shrink_threshold
        )
        if not volume_shrink:
            return cls._rejected_result(
                REJECT_NO_SHRINK,
                pullback_days=pullback_days,
                pullback_depth_pct=round(pullback_depth_pct, 4),
                pullback_low=round(pullback_low, 4),
                volume_shrink_ratio=round(volume_shrink_ratio, 4),
            )

        # 7. 支撑均线选择：MA5 优先
        support_ma = SUPPORT_MA5 if touched_ma5 else SUPPORT_MA10
        support_ma_value = latest_ma5 if touched_ma5 else latest_ma10

        # 8. 企稳确认
        close_back_above_support = latest_close >= support_ma_value
        close_strength = cls._close_strength(latest_high, latest_low, latest_close)
        confirm_candle_bullish = (
            latest_close > latest_open and close_strength >= confirm_close_strength
        )
        prev_high = float(high.iloc[-2]) if len(df) >= 2 else latest_high
        confirm_break_prev_high = (
            latest_close > prev_high or latest_high > prev_high
        )
        rebound_confirmed = close_back_above_support and (
            confirm_candle_bullish or confirm_break_prev_high
        )

        # 9. State
        if not close_back_above_support:
            state = STATE_TOUCH_ONLY
        elif rebound_confirmed:
            state = STATE_CONFIRMED
        else:
            state = STATE_STABILIZING

        # 10. 成熟度建议
        maturity_hint = cls._maturity_hint(state, support_ma)

        # 11. 止损结构化（优先 MA20；MA20 >= close 时退化）
        latest_volume_ratio = float(
            volume.iloc[-1] / volume.iloc[max(-6, -len(volume)):-1].mean()
        ) if len(volume) >= 2 else 0.0

        if latest_ma20 > 0 and latest_ma20 < latest_close:
            stop_loss_price = round(latest_ma20, 4)
            stop_loss_basis = "MA20"
        else:
            stop_loss_price = round(pullback_low * 0.98, 4)
            stop_loss_basis = "pullback_low"

        return {
            "state": state,
            "support_ma": support_ma,
            "maturity_hint": maturity_hint,

            "touched_ma5": bool(touched_ma5),
            "touched_ma10": bool(touched_ma10),
            "pullback_start_bar": pullback_start_bar,
            "pullback_days": pullback_days,
            "pullback_depth_pct": round(pullback_depth_pct, 4),
            "pullback_low": round(pullback_low, 4),
            "pullback_low_bar": pullback_low_bar,

            "volume_shrink_during_pullback": bool(volume_shrink),
            "volume_shrink_ratio": round(volume_shrink_ratio, 4),
            "latest_volume_ratio": round(latest_volume_ratio, 4),

            "close_back_above_support": bool(close_back_above_support),
            "confirm_candle_bullish": bool(confirm_candle_bullish),
            "confirm_break_prev_high": bool(confirm_break_prev_high),
            "rebound_confirmed": bool(rebound_confirmed),

            "entry_price": round(latest_close, 4) if state in (STATE_STABILIZING, STATE_CONFIRMED) else 0.0,
            "support_ma_value": round(support_ma_value, 4),
            "stop_loss_basis": stop_loss_basis,
            "stop_loss_price": stop_loss_price,

            "reject_reason": "",
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _touched_in_window(
        low_series: pd.Series,
        ma_series: pd.Series,
        start_bar: int,
        end_bar: int,
        tolerance: float,
    ) -> bool:
        """回踩窗口内任一 bar 的 low 与当日 MA 距离 <= tolerance。"""
        for idx in range(start_bar, end_bar + 1):
            ma_val = ma_series.iloc[idx] if idx < len(ma_series) else np.nan
            if pd.isna(ma_val) or ma_val <= 0:
                continue
            low_val = float(low_series.iloc[idx])
            if abs(low_val - float(ma_val)) / float(ma_val) <= tolerance:
                return True
        return False

    @staticmethod
    def _close_strength(high: float, low: float, close: float) -> float:
        if high <= low:
            return 0.5
        return (close - low) / (high - low)

    @staticmethod
    def _maturity_hint(state: str, support_ma: str) -> str:
        if state == STATE_CONFIRMED and support_ma == SUPPORT_MA5:
            return MATURITY_HIGH
        if state == STATE_CONFIRMED and support_ma == SUPPORT_MA10:
            return MATURITY_MEDIUM
        if state == STATE_STABILIZING:
            return MATURITY_MEDIUM
        return MATURITY_LOW

    @staticmethod
    def _rejected_result(reason: str, **extra: Any) -> Dict[str, Any]:
        base = {
            "state": STATE_REJECTED,
            "support_ma": SUPPORT_NONE,
            "maturity_hint": MATURITY_LOW,

            "touched_ma5": False,
            "touched_ma10": False,
            "pullback_start_bar": -1,
            "pullback_days": 0,
            "pullback_depth_pct": 0.0,
            "pullback_low": 0.0,
            "pullback_low_bar": -1,

            "volume_shrink_during_pullback": False,
            "volume_shrink_ratio": 0.0,
            "latest_volume_ratio": 0.0,

            "close_back_above_support": False,
            "confirm_candle_bullish": False,
            "confirm_break_prev_high": False,
            "rebound_confirmed": False,

            "entry_price": 0.0,
            "support_ma_value": 0.0,
            "stop_loss_basis": "",
            "stop_loss_price": 0.0,

            "reject_reason": reason,
        }
        base.update(extra)
        return base
