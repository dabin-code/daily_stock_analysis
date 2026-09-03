# -*- coding: utf-8 -*-
"""
低位123检测器（趋势线为可选加分项）。

识别交易结构："低位123反转 — 结构成立 / 观察 / 突破成熟"。
趋势线突破作为信号强度加分项，不作为结构是否成立的门控条件。

设计文档：docs/superpowers/specs/2026-03-24-low-123-trendline-design.md

输出状态说明：
  rejected       — 前置条件不满足（无先行下跌 / 非低位 / P3后破位跌破P1）
  structure_only — 123结构已形成，但最新收盘价仍未站上 P3
  watching       — 123结构已形成，且最新收盘价 > P3 但尚未突破 P2
  breakout_ready — 最新收盘价已突破 P2，次日重点观察买入
"""

from __future__ import annotations

import warnings
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.indicators.divergence_detector import find_swing_highs, find_swing_lows

# ---------------------------------------------------------------------------
# 可调默认参数（均可通过 detect() 的关键字参数覆盖）
# ---------------------------------------------------------------------------
_MIN_BARS = 40
_SWING_ORDER = 3               # 摆动点检测阶数（近端非对称放宽窗口）
_LOOKBACK = 80                 # 候选搜索的最大回溯根数
_LOW_POS_WINDOW = 60           # 判断"低位"时使用的观察窗口
_LOW_POS_RANK = 0.30           # P1 须处于窗口价格区间最低 30% 分位以内
_MIN_BOUNCE_PCT = 0.025        # P1→P2 最小反弹幅度（2.5%）
_MAX_P1_P2_BARS = 30           # P1→P2 最大跨度（根数）：超过则视为松散结构，非同一轮低位123
_MAX_P3_TO_BREAKOUT_BARS = 20  # P2突破距 P3 的最大间隔（根数）：超过则视为陈旧突破，不计为有效买点
_BREAK_TOLERANCE = 0.0         # 破位容差比例：P3后最低价跌破 P1*(1-tol) 才算破位，默认严格跌破
# SOP 5.1 结构止损：设在 P3（或 P1）下方统一缓冲（0.5%），与底背离检测器口径一致
_STRUCTURAL_STOP_BUFFER_PCT = 0.005
# 历史兼容参数：低位123不再以 P2→P3 回撤深度作为硬门槛，
# 仅保留 P3 > P1 的结构要求。detect() 仍保留这两个参数以兼容旧调用。
_MAX_RETRACE_PCT = 0.85
_MIN_RETRACE_PCT = 0.15
_SYNC_WINDOW = 3               # 突破 P2 与突破趋势线的同步容忍窗口（根数）
_TRENDLINE_TOLERANCE = 0.025   # 判断"趋势线接触"的价格容差比例


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


def _check_prior_downtrend(
    df: pd.DataFrame,
    p1_idx: int,
    window: int = 30,
) -> bool:
    """
    检查 P1 之前是否存在有意义的下降趋势。

    以下三条标准满足任意两条即返回 True：
      A) [p1_idx-window, p1_idx] 内最近的 ≥2 个摆动高点呈降序排列。
      B) 接近 P1 时 MA10 < MA20。
      C) [p1_idx-window, p1_idx] 内收盘价的线性斜率为负。
    """
    start = max(0, p1_idx - window)
    segment = df.iloc[start: p1_idx + 1].reset_index(drop=True)
    if len(segment) < 10:
        return False

    close = segment["close"]
    high_col = segment["high"] if "high" in segment.columns else close

    # 条件 A — 摆动高点依次下降
    sh = find_swing_highs(high_col.reset_index(drop=True), order=_SWING_ORDER)
    crit_a = False
    if len(sh) >= 2:
        sh_vals = [float(high_col.iloc[i]) for i in sh]
        crit_a = sh_vals[-1] < sh_vals[-2]

    # 条件 B — MA10 < MA20
    crit_b = False
    if len(close) >= 20:
        ma10 = float(close.iloc[-10:].mean())
        ma20 = float(close.mean())
        crit_b = ma10 < ma20

    # 条件 C — 收盘价斜率为负
    x = np.arange(len(close), dtype=float)
    slope = float(np.polyfit(x, close.values, 1)[0])
    crit_c = slope < 0

    return sum([crit_a, crit_b, crit_c]) >= 2


def _is_low_position(
    df: pd.DataFrame,
    p1_idx: int,
    window: int = _LOW_POS_WINDOW,
    rank_threshold: float = _LOW_POS_RANK,
) -> bool:
    """
    判断 P1 是否处于近期价格区间的低位，确保这是真正的底部反转而非中段整理。

    P1 的最低价须落在近 window 根 K 线收盘区间的最低 rank_threshold 分位内。
    """
    start = max(0, p1_idx - window)
    segment_close = df["close"].iloc[start: p1_idx + 1]
    if len(segment_close) < 5:
        return False

    low_col = df["low"] if "low" in df.columns else df["close"]
    p1_price = float(low_col.iloc[p1_idx])

    seg_min = float(segment_close.min())
    seg_max = float(segment_close.max())
    if seg_max == seg_min:
        return False

    rank = (p1_price - seg_min) / (seg_max - seg_min)
    return rank <= rank_threshold


def _fit_structure_trendline(
    df: pd.DataFrame,
    p1_idx: int,
    p2_idx: int,
    tolerance_pct: float = _TRENDLINE_TOLERANCE,
) -> Dict[str, Any]:
    """
    拟合与当前结构相关的下降压力线。

    只使用 [p1_idx-40, p2_idx] 窗口内的摆动高点，而非全段数据，
    确保趋势线与当前这组低位123结构直接相关。

    返回 downtrend_line 子字典（结构与输出 schema 一致）。
    """
    empty: Dict[str, Any] = {
        "found": False,
        "slope": 0.0,
        "intercept": 0.0,
        "touch_points": [],
        "touch_count": 0,
        "breakout_bar_index": None,
        "projected_value_at_breakout": None,
        "breakout_confirmed": False,
    }

    high_col = df["high"].reset_index(drop=True) if "high" in df.columns else df["close"].reset_index(drop=True)

    # 候选高点范围：P1 前 40 根至 P2（含）
    search_start = max(0, p1_idx - 40)
    sh_all = find_swing_highs(high_col, order=_SWING_ORDER)
    sh_relevant = [i for i in sh_all if search_start <= i <= p2_idx]

    if len(sh_relevant) < 2:
        return empty

    sh_vals = [float(high_col.iloc[i]) for i in sh_relevant]

    # 斜率必须为负才是下降压力线
    slope, intercept = _fit_line(sh_relevant, sh_vals)
    if slope >= 0:
        return empty

    # 统计有效接触点（价格与趋势线偏差在容差内的高点）
    touch_pts: List[Dict[str, Any]] = []
    for idx in sh_relevant:
        line_val = _line_value(slope, intercept, idx)
        if line_val <= 0:
            continue
        actual = float(high_col.iloc[idx])
        if abs(actual - line_val) / abs(line_val) <= tolerance_pct:
            touch_pts.append({"idx": int(idx), "price": round(actual, 4)})

    touch_count = len(touch_pts)
    if touch_count < 2:
        return empty

    return {
        "found": True,
        "slope": round(slope, 6),
        "intercept": round(intercept, 4),
        "touch_points": touch_pts,
        "touch_count": touch_count,
        "breakout_bar_index": None,
        "projected_value_at_breakout": None,
        "breakout_confirmed": False,
    }


def _is_structure_broken_after_p3(
    df: pd.DataFrame,
    p1_val: float,
    p3_idx: int,
    last_idx: int,
    break_tolerance: float = 0.0,
) -> bool:
    """
    判断结构是否在 P3 之后已破位失效。

    低位123一旦形成，P1 是整个结构的最低支撑。若 P3 之后价格再创新低、
    最低价跌破 P1（容差 break_tolerance），则该结构已被后续下跌摧毁，即使
    更晚出现一段与之无关的反弹重新站上 P2，也不能算作本结构的有效突破。

    典型误判：3-4 月形成的123，随后暴跌至远低于 P1，两个月后另一波反弹
    收复旧 P2 价位，旧结构被错误"复活"为 breakout_ready。

    break_tolerance：破位容差比例（0~1）。最低价须跌破 P1*(1-tolerance)
    才算破位，默认 0 表示严格跌破 P1 即失效。
    """
    if p3_idx >= last_idx:
        return False
    low_col = df["low"] if "low" in df.columns else df["close"]
    seg = low_col.iloc[p3_idx + 1: last_idx + 1]
    if len(seg) == 0:
        return False
    return float(seg.min()) < p1_val * (1.0 - break_tolerance)


def _find_breakout_bar(
    close: pd.Series,
    threshold: float,
    search_start: int,
    search_end: int,
) -> Optional[int]:
    """在 [search_start, search_end] 范围内返回第一根收盘 > threshold 的 K 线索引。"""
    for i in range(search_start, min(search_end + 1, len(close))):
        if float(close.iloc[i]) > threshold:
            return i
    return None


def _generate_candidates(
    df: pd.DataFrame,
    lookback: int,
    min_bounce_pct: float,
    max_retrace_pct: float,
    min_retrace_pct: float,
    max_p1_p2_bars: int = _MAX_P1_P2_BARS,
) -> List[Dict[str, Any]]:
    """
    在回溯窗口内生成所有满足条件的 P1/P2/P3 候选三元组。

    每个候选为字典：{p1_idx, p1_val, p2_idx, p2_val, p3_idx, p3_val}
    按 p1_idx 升序排列（最旧在前）；调用方应优先取最新候选。

    关键逻辑：对每个 P1，从最新 P2 候选开始遍历，只有在找到合法 P3 后
    才锁定该 P2。这样可避免末端的突破高点"抢占" P2 槽位，导致真正的
    反弹高点被跳过。

    段内极值约束（603980 风格误判的修复核心）——摆动点必须同时是所在
    区段的极值，不能仅凭"最新"入选：
      - P2 必须是 (P1, P2] 段的最高价：若 P1→P2 之间存在更高的高点，
        说明该 P2 只是真实反弹高点之后的下跌中继反弹，不能作为 P2；
      - (P1, P2) 段内最低价不得跌破 P1：否则 P1 不是本轮结构的真实底部；
      - P3 必须是 (P2, P3] 段的最低价：若 P2→P3 之间存在更低的低点，
        真正的回撤低点在别处，当前摆动低点不是 P3。

    结构紧凑性：P1→P2 跨度超过 ``max_p1_p2_bars`` 的高点会被跳过，
    避免把相隔数月的摆动低点与摆动高点硬凑成同一轮低位123。
    """
    last_idx = len(df) - 1
    cutoff = max(0, last_idx - lookback + 1)

    low_col = df["low"].reset_index(drop=True) if "low" in df.columns else df["close"].reset_index(drop=True)
    high_col = df["high"].reset_index(drop=True) if "high" in df.columns else df["close"].reset_index(drop=True)

    lows = [i for i in find_swing_lows(low_col, order=_SWING_ORDER) if i >= cutoff]
    highs = [i for i in find_swing_highs(high_col, order=_SWING_ORDER) if i >= cutoff]

    candidates: List[Dict[str, Any]] = []

    for p1_idx in lows:
        p1_val = float(low_col.iloc[p1_idx])

        # 从最新 P2 候选开始，只有找到合法 P3 时才锁定 P2
        for h in reversed(highs):
            if h <= p1_idx:
                break  # 后续高点均在 P1 之前，终止
            if h - p1_idx > max_p1_p2_bars:
                continue  # P1→P2 跨度过大，非同一轮结构，跳过该高点
            p2_val = float(high_col.iloc[h])
            if p2_val <= p1_val:
                continue
            bounce_pct = (p2_val - p1_val) / p1_val
            if bounce_pct < min_bounce_pct:
                continue

            interior = slice(p1_idx + 1, h)
            # P2 必须是 P1→P2 段的最高点：中途存在更高高点时，
            # 该 P2 只是下跌中继的反弹高点，跳过。
            if h - p1_idx > 1 and float(high_col.iloc[interior].max()) > p2_val:
                continue
            # P1→P2 段内不得跌破 P1：否则 P1 不是本轮结构的真实底部。
            if h - p1_idx > 1 and float(low_col.iloc[interior].min()) < p1_val:
                continue

            # 寻找该 P2 候选之后最新的合法 P3
            found_p3 = False
            for l in reversed(lows):
                if l <= h:
                    break  # 该 P2 之后无低点，尝试下一个 P2
                p3_val = float(low_col.iloc[l])
                if p3_val <= p1_val:
                    continue
                # P3 必须是 P2→P3 段的最低点：中途存在更低的回撤低点时，
                # 当前摆动低点不是真正的 P3，跳过。
                if p3_val > float(low_col.iloc[h + 1: l + 1].min()):
                    continue
                candidates.append({
                    "p1_idx": p1_idx, "p1_val": p1_val,
                    "p2_idx": int(h), "p2_val": p2_val,
                    "p3_idx": int(l), "p3_val": p3_val,
                })
                found_p3 = True
                break  # 每个 P2 只取最新 P3

            if found_p3:
                break  # 每个 P1 只取最新有效的 (P2, P3) 组合

    return candidates


# ---------------------------------------------------------------------------
# 结果构造辅助函数
# ---------------------------------------------------------------------------

def _rejected(reason: str) -> Dict[str, Any]:
    """构造拒绝状态的标准结果字典。"""
    return {
        "found": False,
        "state": "rejected",
        "rejection_reason": reason,
        "is_low_level": False,
        "point1": None,
        "point2": None,
        "point3": None,
        "downtrend_line": None,
        "breakout_point2_confirmed": False,
        "breakout_trendline_confirmed": False,
        "breakout_p2_bar_index": None,
        "ma_confirmation": False,
        "entry_price": None,
        "stop_loss_price": None,
        "signal_strength": 0.0,
        "pullback_reentry": None,
        "trailing_stop": None,
    }


def _structure_result(
    state: str,
    p1: Dict, p2: Dict, p3: Dict,
    downtrend_line: Optional[Dict],
    bo_p2: bool,
    bo_tl: bool,
    ma_conf: bool,
    entry_price: Optional[float],
    stop_loss: Optional[float],
    signal_strength: float,
    breakout_p2_bar_index: Optional[int] = None,
    pullback_reentry: Optional[Dict[str, Any]] = None,
    trailing_stop: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构造结构已识别状态的标准结果字典。"""
    return {
        "found": True,
        "state": state,
        "rejection_reason": None,
        "is_low_level": True,
        "point1": p1,
        "point2": p2,
        "point3": p3,
        "downtrend_line": downtrend_line,
        "breakout_point2_confirmed": bo_p2,
        "breakout_trendline_confirmed": bo_tl,
        "breakout_p2_bar_index": breakout_p2_bar_index,
        "ma_confirmation": ma_conf,
        "entry_price": entry_price,
        "stop_loss_price": stop_loss,
        "signal_strength": signal_strength,
        "pullback_reentry": pullback_reentry,
        "trailing_stop": trailing_stop,
    }


# ---------------------------------------------------------------------------
# 主检测器类
# ---------------------------------------------------------------------------

class Low123TrendlineDetector:
    """
        低位123 + 下降趋势线联合检测器。

    用法示例::

        result = Low123TrendlineDetector.detect(df)
        if result["state"] == "breakout_ready":
            entry = result["entry_price"]
            stop  = result["stop_loss_price"]
    """

    @classmethod
    def detect(
        cls,
        df: pd.DataFrame,
        lookback: int = _LOOKBACK,
        low_pos_window: int = _LOW_POS_WINDOW,
        low_pos_rank: float = _LOW_POS_RANK,
        min_bounce_pct: float = _MIN_BOUNCE_PCT,
        max_retrace_pct: float = _MAX_RETRACE_PCT,
        min_retrace_pct: float = _MIN_RETRACE_PCT,
        sync_window: int = _SYNC_WINDOW,
        max_p1_p2_bars: int = _MAX_P1_P2_BARS,
        max_breakout_gap: int = _MAX_P3_TO_BREAKOUT_BARS,
        break_tolerance: float = _BREAK_TOLERANCE,
    ) -> Dict[str, Any]:
        """
        识别最近一组有效的"低位123 + 趋势线"结构。

        参数：
            df: 按时间正序排列的 OHLCV DataFrame。
            lookback: 候选结构搜索的最大回溯根数。
            low_pos_window: 低位判断所用的观察窗口根数。
            low_pos_rank: 低位判断的分位阈值（0~1）。
            min_bounce_pct: P1→P2 最小反弹幅度。
            max_retrace_pct: 兼容旧调用，当前不参与结构合法性过滤。
            min_retrace_pct: 兼容旧调用，当前不参与结构合法性过滤。
            sync_window: P2 突破与趋势线突破的同步容忍窗口（根数）。
            max_p1_p2_bars: P1→P2 最大跨度（根数），超过视为松散结构而剔除。
            max_breakout_gap: P2突破距 P3 的最大间隔（根数），超过视为陈旧突破，
                不计为有效买点（结构降级为 watching/structure_only）。
            break_tolerance: 破位容差比例（0~1），P3后最低价跌破 P1*(1-tol) 才算
                破位失效，默认 0 表示严格跌破 P1 即失效。

        返回：
            符合设计文档 §5.6 的结构化字典。
        """
        df = df.reset_index(drop=True)
        if len(df) < _MIN_BARS:
            return _rejected("insufficient_data")

        if max_retrace_pct != _MAX_RETRACE_PCT or min_retrace_pct != _MIN_RETRACE_PCT:
            warnings.warn(
                "max_retrace_pct/min_retrace_pct are deprecated no-op compatibility "
                "parameters; low123 validity now only requires P3 > P1.",
                DeprecationWarning,
                stacklevel=2,
            )
        if sync_window != _SYNC_WINDOW:
            warnings.warn(
                "sync_window is a deprecated no-op compatibility parameter; "
                "trendline breakout is now only a bonus signal.",
                DeprecationWarning,
                stacklevel=2,
            )

        close = df["close"]
        last_idx = len(df) - 1
        low_col = df["low"] if "low" in df.columns else close

        # 第一步：生成所有 P1/P2/P3 候选三元组
        candidates = _generate_candidates(
            df, lookback, min_bounce_pct, max_retrace_pct, min_retrace_pct,
            max_p1_p2_bars,
        )
        if not candidates:
            return _rejected("no_123_structure")

        # 第二步：过滤候选 — 检查前置下跌 + 低位条件
        # 只认最近一组有效结构，旧结构自动失效。
        valid_candidates: List[Dict[str, Any]] = []
        for cand in reversed(candidates):   # 最新 P1 优先
            p1_idx = cand["p1_idx"]

            if not _check_prior_downtrend(df, p1_idx):
                continue

            if not _is_low_position(df, p1_idx, low_pos_window, low_pos_rank):
                cand["reject_reason"] = "not_low_level_position"
                continue

            # P1 必须是近期窗口内的最低点：若 P1 之前不远处存在更低的低价，
            # 真正的底部在别处，当前结构只是下跌中继段拼出的伪123。
            win_start = max(0, p1_idx - low_pos_window)
            if p1_idx > win_start and float(
                low_col.iloc[win_start: p1_idx].min()
            ) < cand["p1_val"]:
                cand["reject_reason"] = "p1_not_lowest_low"
                continue

            # 破位失效：P3 之后最低价跌破 P1，结构已被后续下跌摧毁。
            if _is_structure_broken_after_p3(
                df, cand["p1_val"], cand["p3_idx"], last_idx, break_tolerance,
            ):
                cand["reject_reason"] = "structure_broken_below_p1"
                continue

            cand["reject_reason"] = None
            valid_candidates.append(cand)

        # 所有候选均未通过时，返回最具信息量的拒绝原因
        if not valid_candidates:
            all_reasons = [c.get("reject_reason") for c in reversed(candidates)]
            if any(r == "structure_broken_below_p1" for r in all_reasons):
                return _rejected("structure_broken_below_p1")
            if any(r == "not_low_level_position" for r in all_reasons):
                return _rejected("not_low_level_position")
            if any(r == "p1_not_lowest_low" for r in all_reasons):
                return _rejected("p1_not_lowest_low")
            return _rejected("no_prior_downtrend")

        # 第三步：只评估最近一组有效候选。
        latest_candidate = valid_candidates[0]
        return cls._evaluate_candidate(
            df, close, last_idx, latest_candidate, sync_window, max_breakout_gap,
        )

    # -----------------------------------------------------------------------
    @classmethod
    def _evaluate_candidate(
        cls,
        df: pd.DataFrame,
        close: pd.Series,
        last_idx: int,
        cand: Dict[str, Any],
        sync_window: int,
        max_breakout_gap: int = _MAX_P3_TO_BREAKOUT_BARS,
    ) -> Dict[str, Any]:
        """对单个候选三元组进行趋势线拟合、突破检测和状态判定。"""
        p1_idx, p1_val = cand["p1_idx"], cand["p1_val"]
        p2_idx, p2_val = cand["p2_idx"], cand["p2_val"]
        p3_idx, p3_val = cand["p3_idx"], cand["p3_val"]

        p1 = {"idx": int(p1_idx), "price": round(p1_val, 4)}
        p2 = {"idx": int(p2_idx), "price": round(p2_val, 4)}
        p3 = {"idx": int(p3_idx), "price": round(p3_val, 4)}

        # 拟合结构相关趋势线
        dtl = _fit_structure_trendline(df, p1_idx, p2_idx)

        # 均线确认（辅助加分项）
        ma_conf = cls._check_ma_confirmation(df, last_idx)

        # 检测 P2 突破
        bo_p2_bar = _find_breakout_bar(close, p2_val, p3_idx, last_idx)
        bo_p2 = bo_p2_bar is not None

        # 突破时效：距 P3 过久的突破视为陈旧结构，不计为有效 P2 突破。
        # 这可避免"结构成立后长期未突破，很晚才收复 P2"被误报为最佳买点。
        if bo_p2_bar is not None and (bo_p2_bar - p3_idx) > max_breakout_gap:
            bo_p2 = False
            bo_p2_bar = None

        # 检测趋势线突破（P3 之后第一根收盘 > 趋势线投影值的 K 线）
        bo_tl = False
        bo_tl_bar: Optional[int] = None
        if dtl["found"]:
            for i in range(p3_idx, last_idx + 1):
                tl_val = _line_value(dtl["slope"], dtl["intercept"], i)
                if float(close.iloc[i]) > tl_val:
                    bo_tl_bar = i
                    bo_tl = True
                    dtl["breakout_bar_index"] = int(i)
                    dtl["projected_value_at_breakout"] = round(tl_val, 4)
                    dtl["breakout_confirmed"] = True
                    break

        # 计算信号强度
        strength = cls._compute_signal_strength(
            dtl, bo_p2, bo_tl, ma_conf,
            p3_idx, bo_p2_bar, last_idx,
        )

        latest_close = float(close.iloc[last_idx])

        # 判定状态：
        # - latest_close > P2 → breakout_ready
        # - latest_close > P3 → watching
        # - 其他 → structure_only
        if latest_close > p2_val and bo_p2_bar is not None:
            entry_price = float(close.iloc[bo_p2_bar])
            stop_loss = round(p3_val * (1 - _STRUCTURAL_STOP_BUFFER_PCT), 4)
            pb_reentry, tr_stop = cls._compute_post_breakout_signals(
                df, close, p2_val, p3_val, bo_p2_bar, last_idx,
            )
            return _structure_result(
                "breakout_ready", p1, p2, p3, dtl,
                True, bo_tl, ma_conf,
                round(entry_price, 4), stop_loss, strength,
                breakout_p2_bar_index=int(bo_p2_bar),
                pullback_reentry=pb_reentry,
                trailing_stop=tr_stop,
            )
        if latest_close > p3_val:
            return _structure_result(
                "watching", p1, p2, p3, dtl,
                False, bo_tl, ma_conf,
                None, None, strength,
            )
        else:
            return _structure_result(
                "structure_only", p1, p2, p3, dtl,
                False, bo_tl, ma_conf,
                None, None, strength,
            )

    # -----------------------------------------------------------------------
    @staticmethod
    def _compute_post_breakout_signals(
        df: pd.DataFrame,
        close: pd.Series,
        p2_val: float,
        p3_val: float,
        bo_p2_bar: int,
        last_idx: int,
        pullback_tolerance: float = 0.03,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        计算P2突破后的两个附加信号：回踩支撑 + 动态止损推升。

        回踩支撑（pullback_reentry）：
          突破P2后价格回落至P2附近(±tolerance)并再次企稳（后续收盘>P2）。

        动态止损（trailing_stop，自然止损法）：
          从P3出发，扫描突破后的swing lows，每找到一个更高的低点就上移止损。
        """
        low_col = df["low"] if "low" in df.columns else close

        # --- 回踩支撑检测 ---
        pb_reentry: Dict[str, Any] = {
            "detected": False,
            "support_price": round(p2_val, 4),
            "pullback_low": None,
            "depth_pct": None,
            "bars_since_pullback": None,
        }

        p2_lower = p2_val * (1 - pullback_tolerance)
        scan_start = bo_p2_bar + 1
        if scan_start <= last_idx:
            pullback_low_val = None
            pullback_low_idx = None
            # 找到突破后回落至P2附近的最低点
            for i in range(scan_start, last_idx + 1):
                lo = float(low_col.iloc[i])
                if lo <= p2_val * (1 + pullback_tolerance):
                    if pullback_low_val is None or lo < pullback_low_val:
                        pullback_low_val = lo
                        pullback_low_idx = i

            # 确认回踩后企稳：低点之后至少有一根收盘 > P2
            if pullback_low_val is not None and pullback_low_val >= p2_lower:
                stabilized = False
                for j in range(pullback_low_idx + 1, last_idx + 1):
                    if float(close.iloc[j]) > p2_val:
                        stabilized = True
                        break
                if stabilized:
                    depth = (p2_val - pullback_low_val) / p2_val if p2_val > 0 else 0.0
                    pb_reentry = {
                        "detected": True,
                        "support_price": round(p2_val, 4),
                        "pullback_low": round(pullback_low_val, 4),
                        "depth_pct": round(depth * 100, 2),
                        "bars_since_pullback": last_idx - pullback_low_idx,
                    }

        # --- 动态止损推升（自然止损法） ---
        current_stop = p3_val
        stop_source = "P3"
        stop_idx = None
        upgrades = 0

        # 扫描突破后的swing lows
        swing_low_col = low_col.reset_index(drop=True)
        post_lows = find_swing_lows(swing_low_col, order=_SWING_ORDER)
        for sl_idx in post_lows:
            if sl_idx <= bo_p2_bar:
                continue
            sl_val = float(swing_low_col.iloc[sl_idx])
            if sl_val > current_stop:
                current_stop = sl_val
                stop_source = "swing_low_after_breakout"
                stop_idx = int(sl_idx)
                upgrades += 1

        tr_stop: Dict[str, Any] = {
            "price": round(current_stop, 4),
            "source": stop_source,
            "swing_low_idx": stop_idx,
            "upgrades": upgrades,
        }

        return pb_reentry, tr_stop

    # -----------------------------------------------------------------------
    @staticmethod
    def _check_ma_confirmation(df: pd.DataFrame, last_idx: int) -> bool:
        """最后一根 K 线收盘价站上 MA10 或 MA20 时返回 True。"""
        close = df["close"]
        if len(close) < 20:
            return False
        price = float(close.iloc[last_idx])
        ma10 = float(close.iloc[max(0, last_idx - 9): last_idx + 1].mean())
        ma20 = float(close.iloc[max(0, last_idx - 19): last_idx + 1].mean())
        return price >= ma10 or price >= ma20

    # -----------------------------------------------------------------------
    @staticmethod
    def _compute_signal_strength(
        dtl: Dict[str, Any],
        bo_p2: bool,
        bo_tl: bool,
        ma_conf: bool,
        p3_idx: int,
        bo_p2_bar: Optional[int],
        last_idx: int,
    ) -> float:
        """
        信号强度评分，范围 [0, 1]，越高表示信号越强/越新鲜。

        各分量权重：
          0.40 — P2 突破（核心买入信号）
          0.20 — 趋势线突破（加分项）
          0.10 — 趋势线质量（接触点数量，加分项）
          0.15 — 均线确认
          0.15 — 新鲜度（距 P3 的根数）
        """
        score = 0.0
        if bo_p2:
            score += 0.40
        if bo_tl:
            score += 0.20

        # 趋势线质量加分
        tc = dtl.get("touch_count", 0)
        if tc >= 4:
            score += 0.10
        elif tc == 3:
            score += 0.07
        elif tc == 2:
            score += 0.04

        if ma_conf:
            score += 0.15

        # 新鲜度加分：距 P3 越近分越高
        bars_since_p3 = last_idx - p3_idx
        if bars_since_p3 <= 3:
            score += 0.15
        elif bars_since_p3 <= 8:
            score += 0.10
        elif bars_since_p3 <= 15:
            score += 0.05

        return round(min(score, 1.0), 4)
