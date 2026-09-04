# -*- coding: utf-8 -*-
"""
底背离双突破联合检测器 (Bottom Divergence Double Breakout Detector)。

严格基于 DIF/DEA 黄白线识别六种底背离形态，配合下降趋势线 + 水平阻力线
双突破确认，输出统一结构化结果。

设计文档: docs/superpowers/specs/2026-03-25-bottom-divergence-double-breakout-design.md

输出状态说明:
  rejected        — 前置条件不满足 / 数据不足 / 无有效形态
  divergence_only — 底背离成立但双突破未完成
  structure_ready — 底背离 + 部分突破（仅一侧）
  confirmed       — 双突破同步确认，最终买点成立
  late_or_weak    — 双突破不同步（间隔超出同步窗口）
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.indicators.divergence_detector import (
    DivergenceDetector,
    compute_macd,
    find_swing_highs,
    find_swing_lows,
)
from src.indicators.resistance_zone_detector import ResistanceZoneDetector

# ---------------------------------------------------------------------------
# 可调默认参数（均可通过 detect() kwargs 覆盖）
# ---------------------------------------------------------------------------
_MIN_BARS = 60
_SWING_ORDER = 5
_LOOKBACK = 100
_MIN_AB_GAP = 20            # A/B 最小间隔 bars（约1个月），过滤短促杂波，只认"肉眼可见的清晰波段"
_MAX_AB_GAP = 60            # A/B 最大间隔 bars，避免跨越数月拼接结构
_FLAT_TOLERANCE = 0.05      # 价格 flat 判定容差（5%）
_MACD_FLAT_TOLERANCE = 0.30 # MACD flat 判定容差（30%，DIF/DEA 波动大于价格）
_AB_MATCH_WINDOW = 5        # 价格低点与 DIF/DEA 低点配对窗口
_SYNC_WINDOW = 3            # 双突破同步窗口（bars）
_MIN_BOUNCE_PCT = 0.10      # A→H 最小反弹幅度（10%）
_TRENDLINE_TOLERANCE = 0.025  # 趋势线触点容差
_MACD_FAST = 12
_MACD_SLOW = 26
_MACD_SIGNAL = 9
_CONTEXT_WINDOW = 50        # 上下文门控回溯窗口
_MIN_DECLINE_PCT = 0.15     # A 前上下文内需有 ≥15% 跌幅（过滤横盘震荡）
_LOW_POSITION_PCT = 0.30    # A 须在近60 bar 价格区间最低30%分位
_MIN_DIF_NEGATIVE_RATIO = 0.005  # DIF 须低于 -price*0.5%（过滤零轴噪音）
_MAX_BREAKOUT_GAP = 30      # 双突破距 B 的最大间隔 bars：超过视为陈旧突破，不计双突破确认
_BREAK_TOLERANCE = 0.0      # 破位容差比例（0~1）：B 后最低价跌破 min(A,B)*(1-tol) 视为结构失效
# SOP 5.1 结构止损：设在 A/B 低点下方统一缓冲（0.5%），与低位123检测器口径一致
_STRUCTURAL_STOP_BUFFER_PCT = 0.005

# ---------------------------------------------------------------------------
# 六种有效形态定义
# ---------------------------------------------------------------------------
_VALID_PATTERNS: Dict[Tuple[str, str], Dict[str, str]] = {
    ("down", "up"): {
        "code": "price_down_macd_up",
        "label": "经典底背离",
        "family": "price_down",
    },
    ("down", "flat"): {
        "code": "price_down_macd_flat",
        "label": "价格创新低·MACD持平",
        "family": "price_down",
    },
    ("flat", "up"): {
        "code": "price_flat_macd_up",
        "label": "价格持平·MACD抬升",
        "family": "price_flat",
    },
    ("flat", "down"): {
        "code": "price_flat_macd_down",
        "label": "价格持平·MACD走弱",
        "family": "price_flat",
    },
    ("up", "down"): {
        "code": "price_up_macd_down",
        "label": "强势回撤·MACD走弱",
        "family": "price_up",
    },
    ("up", "flat"): {
        "code": "price_up_macd_flat",
        "label": "强势回撤·MACD持平",
        "family": "price_up",
    },
}

# ---------------------------------------------------------------------------
# 空结果模板
# ---------------------------------------------------------------------------


def _empty_result(
    state: str = "rejected",
    rejection_reason: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "found": False,
        "state": state,
        "pattern_family": None,
        "pattern_code": None,
        "pattern_label": None,
        "price_relation": None,
        "macd_relation": None,
        "price_low_a": None,
        "price_low_b": None,
        "macd_low_a": None,
        "macd_low_b": None,
        "rebound_high": None,
        "horizontal_resistance": None,
        "downtrend_line": None,
        "horizontal_breakout_confirmed": False,
        "trendline_breakout_confirmed": False,
        "double_breakout_sync": False,
        "confirmation_bar_index": None,
        "entry_price": None,
        "stop_loss_price": None,
        "signal_strength": 0.0,
        "rejection_reason": rejection_reason,
        "hit_reasons": [],
        "buy_points": [],
        "exit_plan": None,
    }


# ---------------------------------------------------------------------------
# 内部工具函数
# ---------------------------------------------------------------------------


def _fit_line(indices: List[int], values: List[float]) -> Tuple[float, float]:
    """最小二乘直线拟合，返回 (slope, intercept)。"""
    x = np.array(indices, dtype=float)
    y = np.array(values, dtype=float)
    A = np.vstack([x, np.ones(len(x))]).T
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    return float(coef[0]), float(coef[1])


def _line_value(slope: float, intercept: float, idx: int) -> float:
    return slope * idx + intercept


def _classify_relation(val_a: float, val_b: float, tolerance: float) -> str:
    """三态分类: down / flat / up。"""
    if val_a == 0:
        return "flat"
    ratio = (val_b - val_a) / abs(val_a)
    if ratio < -tolerance:
        return "down"
    if ratio > tolerance:
        return "up"
    return "flat"


def _find_local_min_in_window(
    series: pd.Series, center: int, window: int,
) -> Optional[int]:
    """在 [center-window, center+window] 范围内找局部最小值索引。"""
    start = max(0, center - window)
    end = min(len(series), center + window + 1)
    segment = series.iloc[start:end]
    if segment.empty:
        return None
    local_min_idx = int(segment.idxmin())
    return local_min_idx


def _check_prior_downtrend(
    df: pd.DataFrame,
    ref_idx: int,
    window: int = _CONTEXT_WINDOW,
    swing_order: int = _SWING_ORDER,
) -> bool:
    """
    检查 ref_idx 之前是否存在下跌背景。
    三条标准满足任意两条即返回 True:
      A) 近期 swing highs 呈下降排列
      B) MA10 < MA20
      C) 收盘价线性斜率为负
    """
    start = max(0, ref_idx - window)
    segment = df.iloc[start: ref_idx + 1].reset_index(drop=True)
    if len(segment) < 10:
        return False

    close = segment["close"]
    high_col = segment["high"] if "high" in segment.columns else close
    score = 0

    # A: swing highs 下降
    sh = find_swing_highs(high_col.reset_index(drop=True), order=min(swing_order, 3))
    if len(sh) >= 2:
        vals = [float(high_col.iloc[i]) for i in sh[-2:]]
        if vals[-1] < vals[-2]:
            score += 1

    # B: MA10 < MA20
    if len(close) >= 20:
        ma10 = float(close.tail(10).mean())
        ma20 = float(close.tail(20).mean())
        if ma10 < ma20:
            score += 1

    # C: 线性斜率为负
    x = np.arange(len(close), dtype=float)
    y = close.values.astype(float)
    if len(x) >= 5:
        coef = np.polyfit(x, y, 1)
        if coef[0] < 0:
            score += 1

    return score >= 2


def _check_prior_uptrend(
    df: pd.DataFrame,
    ref_idx: int,
    window: int = _CONTEXT_WINDOW,
    swing_order: int = _SWING_ORDER,
) -> bool:
    """
    检查 ref_idx 之前是否存在上涨背景（用于强势回撤型门控）。
    三条标准满足任意两条即返回 True:
      A) 近期 swing lows 呈上升排列
      B) MA10 > MA20
      C) 收盘价线性斜率为正
    """
    start = max(0, ref_idx - window)
    segment = df.iloc[start: ref_idx + 1].reset_index(drop=True)
    if len(segment) < 10:
        return False

    close = segment["close"]
    low_col = segment["low"] if "low" in segment.columns else close
    score = 0

    # A: swing lows 上升
    sl = find_swing_lows(low_col.reset_index(drop=True), order=min(swing_order, 3))
    if len(sl) >= 2:
        vals = [float(low_col.iloc[i]) for i in sl[-2:]]
        if vals[-1] > vals[-2]:
            score += 1

    # B: MA10 > MA20
    if len(close) >= 20:
        ma10 = float(close.tail(10).mean())
        ma20 = float(close.tail(20).mean())
        if ma10 > ma20:
            score += 1

    # C: 线性斜率为正
    x = np.arange(len(close), dtype=float)
    y = close.values.astype(float)
    if len(x) >= 5:
        coef = np.polyfit(x, y, 1)
        if coef[0] > 0:
            score += 1

    return score >= 2


def _fit_downtrend_line(
    df: pd.DataFrame,
    a_idx: int,
    b_idx: int,
    swing_order: int = _SWING_ORDER,
    tolerance_pct: float = _TRENDLINE_TOLERANCE,
    max_breakout_gap: int = _MAX_BREAKOUT_GAP,
) -> Dict[str, Any]:
    """
    拟合与 A/B 结构相关的下降趋势线。

    使用 [A-20, B+10] 范围内的 swing highs，要求斜率为负、至少 2 个触点。
    突破搜索窗口限制在 B 之后 ``max_breakout_gap`` 根内，超出视为陈旧突破。
    """
    empty = {
        "found": False,
        "slope": 0.0,
        "intercept": 0.0,
        "touch_points": [],
        "touch_count": 0,
        "breakout_bar_index": None,
        "projected_value_at_breakout": None,
        "breakout_confirmed": False,
    }

    search_start = max(0, a_idx - 20)
    search_end = min(len(df), b_idx + 10)
    high_col = df["high"] if "high" in df.columns else df["close"]
    close = df["close"].reset_index(drop=True)

    # 找 swing highs
    segment_high = high_col.iloc[search_start:search_end].reset_index(drop=True)
    if len(segment_high) < 5:
        return empty

    sh_local = find_swing_highs(segment_high, order=min(swing_order, 3))
    if len(sh_local) < 2:
        return empty

    # 转回全局索引
    sh_global = [i + search_start for i in sh_local]
    sh_values = [float(high_col.iloc[i]) for i in sh_global]

    # 拟合
    slope, intercept = _fit_line(sh_global, sh_values)
    if slope >= 0:
        return empty

    # 计算触点
    touch_points = []
    for idx, val in zip(sh_global, sh_values):
        projected = _line_value(slope, intercept, idx)
        if projected > 0 and abs(val - projected) / projected <= tolerance_pct:
            touch_points.append({"idx": idx, "price": val})

    if len(touch_points) < 2:
        return empty

    # 寻找突破点: B 之后第一个 close > trendline 投影值的 bar
    # 突破时效：仅在 B 之后 max_breakout_gap 根内搜索，超出视为陈旧突破。
    breakout_bar = None
    projected_at_breakout = None
    search_after_b = min(len(close), b_idx + 1 + max_breakout_gap)
    for i in range(b_idx + 1, search_after_b):
        proj = _line_value(slope, intercept, i)
        if float(close.iloc[i]) > proj:
            breakout_bar = i
            projected_at_breakout = round(proj, 4)
            break

    return {
        "found": True,
        "slope": round(slope, 6),
        "intercept": round(intercept, 4),
        "touch_points": touch_points,
        "touch_count": len(touch_points),
        "breakout_bar_index": breakout_bar,
        "projected_value_at_breakout": projected_at_breakout,
        "breakout_confirmed": breakout_bar is not None,
    }


# ---------------------------------------------------------------------------
# 主检测器类
# ---------------------------------------------------------------------------


class BottomDivergenceBreakoutDetector:
    """底背离双突破联合检测器 — 基于 DIF/DEA 六形态 + 双突破确认。"""

    @classmethod
    def detect(
        cls,
        df: pd.DataFrame,
        *,
        swing_order: int = _SWING_ORDER,
        lookback: int = _LOOKBACK,
        min_ab_gap: int = _MIN_AB_GAP,
        max_ab_gap: int = _MAX_AB_GAP,
        flat_tolerance: float = _FLAT_TOLERANCE,
        macd_flat_tolerance: float = _MACD_FLAT_TOLERANCE,
        ab_match_window: int = _AB_MATCH_WINDOW,
        sync_window: int = _SYNC_WINDOW,
        trendline_tolerance: float = _TRENDLINE_TOLERANCE,
        macd_fast: int = _MACD_FAST,
        macd_slow: int = _MACD_SLOW,
        macd_signal: int = _MACD_SIGNAL,
        context_window: int = _CONTEXT_WINDOW,
        min_decline_pct: float = _MIN_DECLINE_PCT,
        low_position_pct: float = _LOW_POSITION_PCT,
        min_dif_negative_ratio: float = _MIN_DIF_NEGATIVE_RATIO,
        max_breakout_gap: int = _MAX_BREAKOUT_GAP,
        break_tolerance: float = _BREAK_TOLERANCE,
    ) -> Dict[str, Any]:
        """
        主检测入口。

        Args:
            df: OHLCV DataFrame（需含 close, high, low, volume）
            其余均为可调参数，默认值见模块顶部常量

        Returns:
            统一结构化结果 dict（见设计文档第九节）
        """
        if df is None or len(df) < _MIN_BARS:
            return _empty_result("rejected", "insufficient_data")

        close = df["close"].reset_index(drop=True)
        high_col = (
            df["high"].reset_index(drop=True)
            if "high" in df.columns
            else close.copy()
        )
        low_col = (
            df["low"].reset_index(drop=True)
            if "low" in df.columns
            else close.copy()
        )

        n = len(close)

        # Step 1: 计算 MACD (DIF / DEA)
        dif, dea, _ = compute_macd(close, macd_fast, macd_slow, macd_signal)

        # Step 2: 找价格 swing lows 和 swing highs
        price_swing_lows = find_swing_lows(low_col, order=swing_order)
        price_swing_highs = find_swing_highs(high_col, order=swing_order)

        if len(price_swing_lows) < 2:
            return _empty_result("rejected", "not_enough_swing_lows")

        # Step 3: 生成候选 A/B 对
        candidates = cls._generate_ab_candidates(
            close=close,
            low_col=low_col,
            swing_lows=price_swing_lows,
            swing_highs=price_swing_highs,
            high_col=high_col,
            n=n,
            lookback=lookback,
            min_ab_gap=min_ab_gap,
            max_ab_gap=max_ab_gap,
        )

        if not candidates:
            return _empty_result("rejected", "no_valid_ab_pair")

        # Step 4: 对每个候选做完整评估，取最佳结果
        best_result = None
        best_score = -1.0

        for cand in candidates:
            result = cls._evaluate_candidate(
                df=df,
                close=close,
                high_col=high_col,
                low_col=low_col,
                dif=dif,
                dea=dea,
                cand=cand,
                flat_tolerance=flat_tolerance,
                macd_flat_tolerance=macd_flat_tolerance,
                ab_match_window=ab_match_window,
                sync_window=sync_window,
                trendline_tolerance=trendline_tolerance,
                swing_order=swing_order,
                context_window=context_window,
                min_decline_pct=min_decline_pct,
                low_position_pct=low_position_pct,
                min_dif_negative_ratio=min_dif_negative_ratio,
                max_breakout_gap=max_breakout_gap,
                break_tolerance=break_tolerance,
            )
            if result is None:
                continue
            score = result.get("signal_strength", 0.0)
            # 优先选 confirmed > structure_ready > divergence_only > late_or_weak
            state_priority = {
                "confirmed": 100,
                "late_or_weak": 50,
                "structure_ready": 30,
                "divergence_only": 20,
            }
            # 新鲜度加权：同状态下更靠近当前 K 线的结构优先，避免陈旧结构
            # 因偶然突破旧阻力而压过更贴近当前走势的新结构。
            # 权重 < 1，小于任意两个 state_priority 档位差（最小差 10），
            # 因此仅在同状态内起决定作用，不会跨状态误升级。
            b_idx = cand["b_idx"]
            recency_bonus = 0.9 * (b_idx / max(1, n - 1))
            effective = score + recency_bonus + state_priority.get(result["state"], 0)
            if effective > best_score:
                best_score = effective
                best_result = result

        if best_result is None:
            return _empty_result("rejected", "no_valid_pattern")

        return best_result

    @classmethod
    def _generate_ab_candidates(
        cls,
        *,
        close: pd.Series,
        low_col: pd.Series,
        swing_lows: List[int],
        swing_highs: List[int],
        high_col: pd.Series,
        n: int,
        lookback: int,
        min_ab_gap: int,
        max_ab_gap: int,
    ) -> List[Dict[str, Any]]:
        """生成满足约束的 A/B 低点对候选。"""
        candidates = []
        cutoff = n - lookback

        for i in range(len(swing_lows)):
            for j in range(i + 1, len(swing_lows)):
                a_idx = swing_lows[i]
                b_idx = swing_lows[j]

                # B 足够新
                if b_idx < cutoff:
                    continue

                # 最小间隔
                if b_idx - a_idx < min_ab_gap:
                    continue
                if b_idx - a_idx > max_ab_gap:
                    continue

                a_price = float(low_col.iloc[a_idx])
                b_price = float(low_col.iloc[b_idx])

                # A/B 之间必须有反弹高点 H
                highs_between = [
                    h for h in swing_highs if a_idx < h < b_idx
                ]
                if not highs_between:
                    continue

                # 取最高的反弹点作为 H
                h_idx = max(highs_between, key=lambda h: float(high_col.iloc[h]))
                h_price = float(high_col.iloc[h_idx])

                # H 必须明显高于 A 和 B
                if h_price <= a_price or h_price <= b_price:
                    continue

                # H 必须有足够的反弹幅度
                min_low = min(a_price, b_price)
                if min_low > 0 and (h_price - min_low) / min_low < _MIN_BOUNCE_PCT:
                    continue

                candidates.append({
                    "a_idx": a_idx,
                    "a_price": a_price,
                    "b_idx": b_idx,
                    "b_price": b_price,
                    "h_idx": h_idx,
                    "h_price": h_price,
                })

        return candidates

    @classmethod
    def _evaluate_candidate(
        cls,
        *,
        df: pd.DataFrame,
        close: pd.Series,
        high_col: pd.Series,
        low_col: pd.Series,
        dif: pd.Series,
        dea: pd.Series,
        cand: Dict[str, Any],
        flat_tolerance: float,
        macd_flat_tolerance: float,
        ab_match_window: int,
        sync_window: int,
        trendline_tolerance: float,
        swing_order: int,
        context_window: int,
        min_decline_pct: float,
        low_position_pct: float,
        min_dif_negative_ratio: float,
        max_breakout_gap: int = _MAX_BREAKOUT_GAP,
        break_tolerance: float = _BREAK_TOLERANCE,
    ) -> Optional[Dict[str, Any]]:
        """评估单个 A/B 候选，返回完整结果或 None。"""
        a_idx = cand["a_idx"]
        b_idx = cand["b_idx"]
        a_price = cand["a_price"]
        b_price = cand["b_price"]
        h_idx = cand["h_idx"]
        h_price = cand["h_price"]
        n = len(close)

        # --- 结构破位失效护栏 ---
        # 底背离结构的最低支撑是 min(A, B)。若 B 之后价格再创新低、
        # 跌破该支撑（容差 break_tolerance），则该结构已被后续下跌摧毁，
        # 即使更晚出现一段无关反弹重新收复旧阻力，也不能算本结构的有效突破。
        # 典型误判：结构成立后暴跌至远低于 B，数月后另一波反弹收复旧阻力位，
        # 旧结构被错误「复活」为 confirmed。
        if b_idx < n - 1:
            structure_floor = min(a_price, b_price) * (1.0 - break_tolerance)
            post_b_low = float(low_col.iloc[b_idx + 1:].min())
            if post_b_low < structure_floor:
                return None

        # --- 计算前置跌幅（用于过滤 + 命中原因） ---
        ctx_start = max(0, a_idx - context_window)
        ctx_high = float(high_col.iloc[ctx_start: a_idx + 1].max())
        decline_pct = (ctx_high - a_price) / ctx_high if ctx_high > 0 else 0.0

        # --- 前置过滤1: A 点前须有足够跌幅（过滤横盘震荡） ---
        if decline_pct < min_decline_pct:
            return None  # 前置跌幅不足，非真正底部

        # --- 前置过滤2: A 点须在近期价格区间低位 ---
        range_start = max(0, a_idx - 60)
        range_close = close.iloc[range_start: a_idx + 1]
        if len(range_close) >= 10:
            range_min = float(range_close.min())
            range_max = float(range_close.max())
            if range_max > range_min:
                position = (a_price - range_min) / (range_max - range_min)
                if position > low_position_pct:
                    return None  # A 不在低位区间

        # --- 匹配 DIF/DEA 低点 ---
        dif_a_idx = _find_local_min_in_window(dif, a_idx, ab_match_window)
        dea_a_idx = _find_local_min_in_window(dea, a_idx, ab_match_window)
        dif_b_idx = _find_local_min_in_window(dif, b_idx, ab_match_window)
        dea_b_idx = _find_local_min_in_window(dea, b_idx, ab_match_window)

        if any(v is None for v in (dif_a_idx, dea_a_idx, dif_b_idx, dea_b_idx)):
            return None

        dif_a_val = float(dif.iloc[dif_a_idx])
        dea_a_val = float(dea.iloc[dea_a_idx])
        dif_b_val = float(dif.iloc[dif_b_idx])
        dea_b_val = float(dea.iloc[dea_b_idx])

        # --- 前置过滤3: DIF 须在负值区域（零轴噪音过滤） ---
        # 底背离要求 MACD 处于空头区域，DIF_A 必须显著低于零
        dif_threshold = -a_price * min_dif_negative_ratio
        if dif_a_val > dif_threshold:
            return None  # DIF 在零轴附近或正值，非底背离环境

        # DIF 和 DEA 方向一致性检查
        dif_rel = _classify_relation(dif_a_val, dif_b_val, macd_flat_tolerance)
        dea_rel = _classify_relation(dea_a_val, dea_b_val, macd_flat_tolerance)

        # DIF/DEA 方向冲突（一个 up 一个 down）则拒绝
        conflict = (
            (dif_rel == "up" and dea_rel == "down")
            or (dif_rel == "down" and dea_rel == "up")
        )
        if conflict:
            return None

        # 取 DIF 的关系作为 macd_relation（DEA 应与之一致或 flat）
        macd_relation = dif_rel
        if dif_rel == "flat":
            macd_relation = dea_rel

        # --- 三态分类 ---
        price_relation = _classify_relation(a_price, b_price, flat_tolerance)

        # --- 映射形态 ---
        pattern_key = (price_relation, macd_relation)
        pattern_info = _VALID_PATTERNS.get(pattern_key)
        if pattern_info is None:
            return None  # 不在六种有效形态内

        pattern_code = pattern_info["code"]
        pattern_label = pattern_info["label"]
        pattern_family = pattern_info["family"]

        # --- 上下文门控 ---
        if pattern_family in ("price_down", "price_flat"):
            if not _check_prior_downtrend(df, a_idx, context_window, swing_order):
                return _empty_result("rejected", "no_prior_downtrend") | {
                    "found": False,
                    "pattern_family": pattern_family,
                    "pattern_code": pattern_code,
                    "price_relation": price_relation,
                    "macd_relation": macd_relation,
                }
        elif pattern_family == "price_up":
            if not _check_prior_uptrend(df, a_idx, context_window, swing_order):
                return _empty_result("rejected", "no_prior_uptrend") | {
                    "found": False,
                    "pattern_family": pattern_family,
                    "pattern_code": pattern_code,
                    "price_relation": price_relation,
                    "macd_relation": macd_relation,
                }

        # --- 底背离成立 ---
        macd_low_a = {
            "idx": int(dif_a_idx),
            "dif": round(dif_a_val, 6),
            "dea": round(dea_a_val, 6),
        }
        macd_low_b = {
            "idx": int(dif_b_idx),
            "dif": round(dif_b_val, 6),
            "dea": round(dea_b_val, 6),
        }

        # --- 双突破检查 ---
        # 突破时效护栏：双突破须在 B 之后 max_breakout_gap 根内完成，
        # 否则视为陈旧突破（结构成立后长期未突破、很晚才收复阻力），
        # 不计为有效突破，避免被误报为最佳买点。
        breakout_search_end = min(n, b_idx + 1 + max_breakout_gap)

        # 1. 水平阻力线突破
        horizontal_resistance = h_price
        h_breakout_bar = None
        for i in range(b_idx + 1, breakout_search_end):
            if float(close.iloc[i]) > horizontal_resistance:
                h_breakout_bar = i
                break
        h_breakout_confirmed = h_breakout_bar is not None

        # 2. 下降趋势线突破
        dtl = _fit_downtrend_line(
            df, a_idx, b_idx,
            swing_order=swing_order,
            tolerance_pct=trendline_tolerance,
            max_breakout_gap=max_breakout_gap,
        )
        tl_breakout_confirmed = dtl["breakout_confirmed"]
        tl_breakout_bar = dtl.get("breakout_bar_index")

        # 3. 同步判定
        double_sync = False
        state = "divergence_only"

        if h_breakout_confirmed and tl_breakout_confirmed:
            gap = abs(h_breakout_bar - tl_breakout_bar)
            if gap <= sync_window:
                double_sync = True
                state = "confirmed"
            else:
                state = "late_or_weak"
        elif h_breakout_confirmed or tl_breakout_confirmed:
            state = "structure_ready"

        # --- 入场价与止损 ---
        confirmation_bar = None
        if h_breakout_bar is not None and tl_breakout_bar is not None:
            confirmation_bar = max(h_breakout_bar, tl_breakout_bar)
        elif h_breakout_bar is not None:
            confirmation_bar = h_breakout_bar
        elif tl_breakout_bar is not None:
            confirmation_bar = tl_breakout_bar

        entry_price = None
        stop_loss_price = None
        # SOP 5.1 要求止损设在"新低点下方"，统一 0.5% 缓冲（与低位123口径一致）
        initial_stop_loss = round(min(a_price, b_price) * (1 - _STRUCTURAL_STOP_BUFFER_PCT), 4)

        # --- 止盈结构触发（SOP 5.2）：远期强阻力位 + 红柱高位顶背离 ---
        # 顶背离：价格 higher-high + MACD 红柱 lower-high（DivergenceDetector.detect_bearish）
        top_divergence = False
        try:
            df_for_div = df[["close"]].copy()
            if "high" in df.columns:
                df_for_div["high"] = df["high"].values
            if "low" in df.columns:
                df_for_div["low"] = df["low"].values
            top_divergence = bool(
                DivergenceDetector.detect_bearish(df_for_div).get("found", False)
            )
        except Exception:
            top_divergence = False

        # 远期强阻力：R1 阻力区上沿（有市场记忆的供给区，SOP 5.2"强阻力"）
        far_resistance = None
        try:
            rz = ResistanceZoneDetector.calculate(
                df,
                a_idx=a_idx,
                b_idx=b_idx,
                candidate_version="bottom-divergence-v1",
            )
            r1_zone = rz.get("r1")
            if r1_zone:
                far_resistance = r1_zone.get("upper") or r1_zone.get("upper_body")
        except Exception:
            far_resistance = None

        if state == "confirmed":
            entry_bar = confirmation_bar
            entry_price = round(float(close.iloc[entry_bar]), 4)
            stop_loss_price = initial_stop_loss

        # --- 三级买点（书中：递进买入） ---
        tl_proj = dtl.get("projected_value_at_breakout")
        buy_points = cls._build_buy_points(
            tl_breakout_bar=tl_breakout_bar,
            tl_breakout_confirmed=tl_breakout_confirmed,
            tl_projected_price=tl_proj,
            h_breakout_bar=h_breakout_bar,
            h_breakout_confirmed=h_breakout_confirmed,
            h_price=h_price,
            initial_stop_loss=initial_stop_loss,
            close=close,
            low_col=df["low"].reset_index(drop=True) if "low" in df.columns else close,
        )

        # --- 退出计划（SOP 5.2：结构止盈 + 自然止损法） ---
        # 首目标：远期强阻力（R1 上沿）优先；无 R1 或 R1 不高于入场价时回退 +10%
        tp_first_price = (
            far_resistance
            if (far_resistance and entry_price and far_resistance > entry_price)
            else None
        )
        take_profit_targets = []
        if tp_first_price:
            take_profit_targets.append({
                "price": round(tp_first_price, 4),
                "action": "减半仓",
                "label": "远期强阻力(R1)",
            })
        else:
            take_profit_targets.append({"pct": 10, "action": "减半仓", "label": "首目标+10%"})
        take_profit_targets.append({"pct": 20, "action": "再减1/4", "label": "次目标+20%"})
        take_profit_targets.append({"remaining": True, "action": "跌破最近低点清仓", "label": "尾仓跟踪止损"})

        exit_plan = {
            "initial_stop_loss": initial_stop_loss,
            "critical_zone_lower": round(min(
                tl_proj or initial_stop_loss,
                h_price * 0.97,
            ), 4),
            "take_profit_targets": take_profit_targets,
            "take_profit_resistance": far_resistance,
            "top_divergence": top_divergence,
            "invalidation_days": 3,
            "trailing_stop_method": "natural_stop_loss",
        }

        # --- 信号强度 ---
        signal_strength = cls._compute_signal_strength(
            dtl=dtl,
            h_breakout_confirmed=h_breakout_confirmed,
            tl_breakout_confirmed=tl_breakout_confirmed,
            double_sync=double_sync,
            a_price=a_price,
            b_price=b_price,
            dif_a=dif_a_val,
            dif_b=dif_b_val,
            df=df,
            b_idx=b_idx,
        )

        # --- 命中原因 ---
        hit_reasons = cls._build_hit_reasons(
            df=df,
            pattern_label=pattern_label,
            pattern_code=pattern_code,
            price_relation=price_relation,
            macd_relation=macd_relation,
            a_idx=a_idx,
            b_idx=b_idx,
            a_price=a_price,
            b_price=b_price,
            h_idx=h_idx,
            h_price=h_price,
            dif_a_val=dif_a_val,
            dea_a_val=dea_a_val,
            dif_b_val=dif_b_val,
            dea_b_val=dea_b_val,
            ctx_high=ctx_high,
            decline_pct=decline_pct,
            dtl=dtl,
            horizontal_resistance=horizontal_resistance,
            h_breakout_bar=h_breakout_bar,
            h_breakout_confirmed=h_breakout_confirmed,
            tl_breakout_confirmed=tl_breakout_confirmed,
            tl_breakout_bar=tl_breakout_bar,
            double_sync=double_sync,
            state=state,
            signal_strength=signal_strength,
            buy_points=buy_points,
            exit_plan=exit_plan,
        )

        return {
            "found": True,
            "state": state,
            "pattern_family": pattern_family,
            "pattern_code": pattern_code,
            "pattern_label": pattern_label,
            "price_relation": price_relation,
            "macd_relation": macd_relation,
            "price_low_a": {"idx": int(a_idx), "price": round(a_price, 4)},
            "price_low_b": {"idx": int(b_idx), "price": round(b_price, 4)},
            "macd_low_a": macd_low_a,
            "macd_low_b": macd_low_b,
            "rebound_high": {"idx": int(h_idx), "price": round(h_price, 4)},
            "horizontal_resistance": round(horizontal_resistance, 4),
            "downtrend_line": dtl,
            "horizontal_breakout_confirmed": h_breakout_confirmed,
            "trendline_breakout_confirmed": tl_breakout_confirmed,
            "double_breakout_sync": double_sync,
            "confirmation_bar_index": confirmation_bar,
            "entry_price": entry_price,
            "stop_loss_price": stop_loss_price,
            "signal_strength": round(signal_strength, 4),
            "rejection_reason": None,
            "hit_reasons": hit_reasons,
            "buy_points": buy_points,
            "exit_plan": exit_plan,
        }

    @classmethod
    def _build_buy_points(
        cls,
        *,
        tl_breakout_bar: Optional[int],
        tl_breakout_confirmed: bool,
        tl_projected_price: Optional[float],
        h_breakout_bar: Optional[int],
        h_breakout_confirmed: bool,
        h_price: float,
        initial_stop_loss: float,
        close: pd.Series,
        low_col: pd.Series,
    ) -> List[Dict[str, Any]]:
        """构建书中三级递进买点。

        买点1: 趋势线突破（轻仓试探，1/5仓）
        买点2: 阻力线突破（确认加仓，1/3仓）
        买点3: 回踩支撑（再加仓，1/3仓）— 突破阻力线后回踩原阻力位
        """
        buy_points: List[Dict[str, Any]] = []

        # 买点1 — 趋势线突破
        bp1_price = round(tl_projected_price, 4) if tl_projected_price else None
        buy_points.append({
            "level": 1,
            "label": "趋势线突破",
            "trigger_price": bp1_price,
            "trigger_bar_index": tl_breakout_bar,
            "triggered": tl_breakout_confirmed,
            "position_ratio": "1/5仓",
            "stop_loss_price": initial_stop_loss,
        })

        # 买点2 — 阻力线突破
        # 止损设在趋势线与阻力线临界区域下沿
        bp2_stop = round(min(
            h_price * 0.97,
            tl_projected_price * 0.97 if tl_projected_price else h_price * 0.97,
        ), 4)
        buy_points.append({
            "level": 2,
            "label": "阻力线突破",
            "trigger_price": round(h_price, 4),
            "trigger_bar_index": h_breakout_bar,
            "triggered": h_breakout_confirmed,
            "position_ratio": "1/3仓",
            "stop_loss_price": bp2_stop,
        })

        # 买点3 — 回踩支撑
        pullback = None
        if h_breakout_confirmed and h_breakout_bar is not None:
            pullback = cls._detect_pullback_support(
                close=close,
                low_col=low_col,
                resistance_price=h_price,
                breakout_bar=h_breakout_bar,
            )
        buy_points.append({
            "level": 3,
            "label": "回踩支撑",
            "trigger_price": round(pullback["price"], 4) if pullback else round(h_price, 4),
            "trigger_bar_index": pullback["bar_index"] if pullback else None,
            "triggered": pullback is not None,
            "position_ratio": "1/3仓",
            "stop_loss_price": round(h_price * 0.97, 4),
        })

        return buy_points

    @staticmethod
    def _detect_pullback_support(
        *,
        close: pd.Series,
        low_col: pd.Series,
        resistance_price: float,
        breakout_bar: int,
        scan_window: int = 20,
        tolerance: float = 0.02,
    ) -> Optional[Dict[str, Any]]:
        """检测突破阻力线后的回踩支撑买点。

        书中描述：突破阻力线后回踩，在原阻力线（现支撑线）附近企稳买入。
        条件：bar.low 回到阻力线附近（±容差），且 bar.close 站稳阻力线上方。
        """
        upper_bound = min(len(close), breakout_bar + scan_window)
        for i in range(breakout_bar + 1, upper_bound):
            bar_low = float(low_col.iloc[i])
            bar_close = float(close.iloc[i])
            # 回踩到阻力线附近（下探到阻力线 × (1 + tolerance) 以内）
            if bar_low <= resistance_price * (1 + tolerance):
                # 且收盘站稳阻力线上方
                if bar_close >= resistance_price:
                    return {"bar_index": i, "price": round(bar_close, 4)}
        return None

    @staticmethod
    def _get_date_str(df: pd.DataFrame, idx: int) -> str:
        """尝试从 df 的 date/trade_date 列获取日期字符串，无则返回 bar 索引。"""
        for col in ("date", "trade_date"):
            if col in df.columns:
                val = df[col].iloc[idx] if idx < len(df) else None
                if val is not None:
                    return str(val)[:10]
        return f"第{idx}根K线"

    @staticmethod
    def _build_hit_reasons(
        *,
        df: pd.DataFrame,
        pattern_label: str,
        pattern_code: str,
        price_relation: str,
        macd_relation: str,
        a_idx: int,
        b_idx: int,
        a_price: float,
        b_price: float,
        h_idx: int,
        h_price: float,
        dif_a_val: float,
        dea_a_val: float,
        dif_b_val: float,
        dea_b_val: float,
        ctx_high: float,
        decline_pct: float,
        dtl: Dict[str, Any],
        horizontal_resistance: float,
        h_breakout_bar: Optional[int],
        h_breakout_confirmed: bool,
        tl_breakout_confirmed: bool,
        tl_breakout_bar: Optional[int],
        double_sync: bool,
        state: str,
        signal_strength: float,
        buy_points: Optional[List[Dict[str, Any]]] = None,
        exit_plan: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """构建命中原因列表（中文描述）。"""
        reasons: List[str] = []
        _date = BottomDivergenceBreakoutDetector._get_date_str

        # 1. 底背离形态描述
        a_date = _date(df, a_idx)
        b_date = _date(df, b_idx)
        price_desc = {"down": "更低", "flat": "持平", "up": "更高"}
        macd_desc = {"down": "更低", "flat": "持平", "up": "更高"}
        reasons.append(
            f"【底背离形态】{pattern_label}（{pattern_code}）："
            f"A点({a_date}, 价格{a_price:.2f}) → B点({b_date}, 价格{b_price:.2f})，"
            f"价格{price_desc.get(price_relation, price_relation)}，"
            f"DIF从{dif_a_val:.4f}到{dif_b_val:.4f}（{macd_desc.get(macd_relation, macd_relation)}）"
        )

        # 2. 前置跌幅
        reasons.append(
            f"【前置跌幅】A点前最高价{ctx_high:.2f}，"
            f"到A点低价{a_price:.2f}，跌幅{decline_pct:.1%}"
        )

        # 3. 反弹高点
        h_date = _date(df, h_idx)
        reasons.append(
            f"【反弹高点】H点({h_date}, 价格{h_price:.2f})，"
            f"构成水平阻力线"
        )

        # 4. 下降趋势线
        if dtl.get("found"):
            tp_count = dtl.get("touch_count", 0)
            slope = dtl.get("slope", 0.0)
            touch_descs = []
            for tp in dtl.get("touch_points", [])[:3]:
                tp_date = _date(df, tp["idx"])
                touch_descs.append(f"{tp_date}({tp['price']:.2f})")
            tp_str = "、".join(touch_descs) if touch_descs else "无"
            tl_status = "已突破" if tl_breakout_confirmed else "未突破"
            tl_detail = ""
            if tl_breakout_confirmed and tl_breakout_bar is not None:
                tl_date = _date(df, tl_breakout_bar)
                proj = dtl.get("projected_value_at_breakout", 0)
                tl_detail = f"，突破于{tl_date}（趋势线投影价{proj:.2f}）"
            reasons.append(
                f"【下降趋势线】斜率{slope:.6f}，{tp_count}个触点（{tp_str}），"
                f"状态：{tl_status}{tl_detail}"
            )
        else:
            reasons.append("【下降趋势线】未能拟合有效下降趋势线")

        # 5. 水平阻力线突破
        if h_breakout_confirmed and h_breakout_bar is not None:
            hb_date = _date(df, h_breakout_bar)
            reasons.append(
                f"【水平阻力线】阻力位{horizontal_resistance:.2f}，"
                f"于{hb_date}突破"
            )
        else:
            reasons.append(
                f"【水平阻力线】阻力位{horizontal_resistance:.2f}，尚未突破"
            )

        # 6. 双突破同步判定
        if double_sync:
            gap = 0
            if h_breakout_bar is not None and tl_breakout_bar is not None:
                gap = abs(h_breakout_bar - tl_breakout_bar)
            reasons.append(
                f"【双突破同步】水平阻力线与下降趋势线在{gap}根K线内同步突破，"
                f"状态：{state}"
            )
        elif state == "late_or_weak":
            gap = 0
            if h_breakout_bar is not None and tl_breakout_bar is not None:
                gap = abs(h_breakout_bar - tl_breakout_bar)
            reasons.append(
                f"【双突破不同步】两线突破间隔{gap}根K线，超出同步窗口，"
                f"状态：{state}"
            )
        elif state == "structure_ready":
            which = "水平阻力线" if h_breakout_confirmed else "下降趋势线"
            reasons.append(
                f"【部分突破】仅{which}突破完成，状态：{state}"
            )
        else:
            reasons.append(f"【双突破】均未突破，状态：{state}")

        # 7. 信号强度
        reasons.append(f"【信号强度】综合评分 {signal_strength:.4f}")

        # 8. 三级买点状态
        if buy_points:
            for bp in buy_points:
                status = "已触发" if bp.get("triggered") else "待触发"
                tp = bp.get("trigger_price")
                tp_str = f"{tp:.2f}" if tp is not None else "待定"
                sl = bp.get("stop_loss_price", 0)
                reasons.append(
                    f"【买点{bp['level']}】{bp['label']}：{status}，"
                    f"触发价{tp_str}，仓位{bp['position_ratio']}，止损{sl:.2f}"
                )

        # 9. 退出计划
        if exit_plan:
            isl = exit_plan.get("initial_stop_loss", 0)
            reasons.append(
                f"【止损计划】初始止损{isl:.2f}（新低点下方3%），"
                f"方法：自然止损法（逐步上移）"
            )

        return reasons

    @staticmethod
    def _compute_signal_strength(
        *,
        dtl: Dict[str, Any],
        h_breakout_confirmed: bool,
        tl_breakout_confirmed: bool,
        double_sync: bool,
        a_price: float,
        b_price: float,
        dif_a: float,
        dif_b: float,
        df: pd.DataFrame,
        b_idx: int,
    ) -> float:
        """计算信号强度 [0, 1]。"""
        score = 0.0

        # 趋势线触点质量 (0-0.25)
        touch_count = dtl.get("touch_count", 0)
        score += min(0.25, touch_count * 0.08)

        # 双突破同步 (0.3)
        if double_sync:
            score += 0.3
        elif h_breakout_confirmed and tl_breakout_confirmed:
            score += 0.15  # late_or_weak
        elif h_breakout_confirmed or tl_breakout_confirmed:
            score += 0.05

        # 背离强度: DIF 偏离幅度 (0-0.2)
        if dif_a != 0:
            div_strength = abs(dif_b - dif_a) / abs(dif_a)
            score += min(0.2, div_strength * 0.1)

        # 量比确认 (0-0.15)
        if len(df) > b_idx + 1 and "volume" in df.columns:
            vol = df["volume"].reset_index(drop=True)
            avg_vol = float(vol.iloc[max(0, b_idx - 10):b_idx].mean()) if b_idx > 0 else 0
            curr_vol = float(vol.iloc[min(len(vol) - 1, b_idx + 1)])
            if avg_vol > 0:
                vol_ratio = curr_vol / avg_vol
                score += min(0.15, (vol_ratio - 1.0) * 0.1) if vol_ratio > 1.0 else 0.0

        # 结构清晰度: A/B 价差占比 (0-0.1)
        mid_price = (a_price + b_price) / 2.0
        if mid_price > 0:
            price_diff_pct = abs(a_price - b_price) / mid_price
            score += min(0.1, price_diff_pct * 0.5)

        return min(1.0, max(0.0, score))
