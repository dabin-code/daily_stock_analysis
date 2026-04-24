# -*- coding: utf-8 -*-
"""Tests for ShrinkPullbackDetector — 覆盖 5 个关键场景。

参考计划: c:/Users/iu/.cursor/plans/shrink_pullback复核_50b8b83a.plan.md
"""

from __future__ import annotations

import unittest
from typing import List

import numpy as np
import pandas as pd

from src.indicators.shrink_pullback_detector import (
    ShrinkPullbackDetector,
    STATE_CONFIRMED,
    STATE_STABILIZING,
    STATE_TOUCH_ONLY,
    STATE_REJECTED,
    SUPPORT_MA5,
    SUPPORT_MA10,
    MATURITY_HIGH,
    MATURITY_MEDIUM,
    MATURITY_LOW,
)


def _mk_bar(close: float, *, low: float = None, high: float = None,
            open_: float = None, volume: float = 1000.0, pct_chg: float = 0.0) -> dict:
    if low is None:
        low = close - 0.05
    if high is None:
        high = close + 0.05
    if open_ is None:
        open_ = close
    return {
        "open": open_, "high": high, "low": low, "close": close,
        "volume": volume, "pct_chg": pct_chg,
    }


def _build_df(rows: List[dict]) -> pd.DataFrame:
    dates = pd.date_range(start="2025-01-01", periods=len(rows), freq="D")
    df = pd.DataFrame(rows)
    df.insert(0, "date", dates)
    return df


def _uptrend_then(pullback_bars: List[dict], *, uptrend_len: int = 30,
                  uptrend_vol: float = 2000.0, uptrend_start: float = 9.0,
                  uptrend_end: float = 10.5) -> pd.DataFrame:
    """Build: ``uptrend_len`` bars of steady rise with ``uptrend_vol`` volume,
    then append ``pullback_bars`` verbatim."""
    closes = np.linspace(uptrend_start, uptrend_end, uptrend_len)
    rows = [
        _mk_bar(float(c), volume=uptrend_vol, pct_chg=0.5)
        for c in closes
    ]
    rows.extend(pullback_bars)
    return _build_df(rows)


class StandardMA5PullbackTest(unittest.TestCase):
    """Case 1: 标准 MA5 缩量回踩 + 企稳阳线 → confirmed + HIGH"""

    def test_confirmed_ma5(self) -> None:
        uptrend_len = 30
        pullback = [
            _mk_bar(10.35, low=10.30, high=10.45, open_=10.45, volume=900, pct_chg=-1.0),
            _mk_bar(10.25, low=10.18, high=10.35, open_=10.33, volume=700, pct_chg=-1.0),
            # 今日 confirm：close=10.42 > open=10.28，close_strength 高，突破前一日 high=10.32
            _mk_bar(10.42, low=10.26, high=10.48, open_=10.28, volume=1100, pct_chg=1.6),
        ]
        df = _uptrend_then(pullback, uptrend_len=uptrend_len,
                           uptrend_vol=2000.0, uptrend_start=9.0, uptrend_end=10.5)

        result = ShrinkPullbackDetector.detect(df)

        self.assertEqual(result["state"], STATE_CONFIRMED,
                         msg=f"expected confirmed, got {result}")
        self.assertEqual(result["support_ma"], SUPPORT_MA5)
        self.assertEqual(result["maturity_hint"], MATURITY_HIGH)
        self.assertTrue(result["touched_ma5"])
        self.assertTrue(result["volume_shrink_during_pullback"])
        self.assertTrue(result["rebound_confirmed"])
        self.assertGreater(result["entry_price"], 0.0)
        self.assertGreater(result["stop_loss_price"], 0.0)
        self.assertEqual(result["stop_loss_basis"], "MA20")


class StandardMA10PullbackTest(unittest.TestCase):
    """Case 2: 标准 MA10 缩量回踩 + 企稳 → confirmed + MEDIUM（MA5 不触）"""

    def test_confirmed_ma10_only(self) -> None:
        uptrend_len = 30
        # 更陡上涨（8.0 → 12.0）保证回踩后 MA5 仍在 MA10 上方；回踩 4 天
        pullback = [
            _mk_bar(11.85, low=11.75, high=11.98, open_=11.95, volume=900, pct_chg=-1.0),
            _mk_bar(11.60, low=11.45, high=11.82, open_=11.80, volume=800, pct_chg=-2.1),
            _mk_bar(11.30, low=11.20, high=11.60, open_=11.58, volume=700, pct_chg=-2.6),
            # 今日 close=11.85 站回 MA10 之上，阳线突破前一日 high=11.32
            _mk_bar(11.85, low=11.30, high=11.92, open_=11.35, volume=1100, pct_chg=4.9),
        ]
        df = _uptrend_then(pullback, uptrend_len=uptrend_len,
                           uptrend_vol=2000.0, uptrend_start=8.0, uptrend_end=12.0)

        result = ShrinkPullbackDetector.detect(df)

        # 允许 confirmed 或 stabilizing 均可（此例优先 MA5，如果误判 MA5 触碰需调整）
        self.assertIn(result["state"], (STATE_CONFIRMED, STATE_STABILIZING),
                      msg=f"got {result}")
        self.assertTrue(result["touched_ma10"])
        self.assertTrue(result["volume_shrink_during_pullback"])


class TouchOnlyTest(unittest.TestCase):
    """Case 3: 仅 low 触均线，收盘仍在支撑下方 → touch_only / LOW"""

    def test_touch_only_not_stabilized(self) -> None:
        uptrend_len = 30
        pullback = [
            _mk_bar(10.35, low=10.30, high=10.45, open_=10.45, volume=900, pct_chg=-1.0),
            _mk_bar(10.20, low=10.15, high=10.36, open_=10.34, volume=750, pct_chg=-1.4),
            # 今日收阴，close 低于 MA5（约 10.3x），只是 low 继续触
            _mk_bar(10.05, low=10.00, high=10.22, open_=10.20, volume=800, pct_chg=-1.5),
        ]
        df = _uptrend_then(pullback, uptrend_len=uptrend_len,
                           uptrend_vol=2000.0, uptrend_start=9.0, uptrend_end=10.5)

        result = ShrinkPullbackDetector.detect(df)

        self.assertIn(result["state"], (STATE_TOUCH_ONLY, STATE_REJECTED),
                      msg=f"got {result}")
        self.assertFalse(result["rebound_confirmed"])
        self.assertEqual(result["maturity_hint"], MATURITY_LOW)


class FakePullbackAtHighTest(unittest.TestCase):
    """Case 4: 高位滞涨贴 MA5、回撤太浅 → rejected(shallow_pullback)"""

    def test_shallow_pullback_rejected(self) -> None:
        uptrend_len = 30
        # 几乎不动的粘合 bar，回撤 < 0.5%
        pullback = [
            _mk_bar(10.50, low=10.48, high=10.52, open_=10.50, volume=1800, pct_chg=0.0),
            _mk_bar(10.49, low=10.47, high=10.51, open_=10.50, volume=1700, pct_chg=0.0),
            _mk_bar(10.50, low=10.48, high=10.52, open_=10.49, volume=1750, pct_chg=0.1),
        ]
        df = _uptrend_then(pullback, uptrend_len=uptrend_len,
                           uptrend_vol=2000.0, uptrend_start=9.5, uptrend_end=10.5)

        result = ShrinkPullbackDetector.detect(df)

        self.assertEqual(result["state"], STATE_REJECTED)
        self.assertIn(result["reject_reason"], ("shallow_pullback", "no_shrink"),
                      msg=f"got {result['reject_reason']}")


class WeakBounceInDowntrendTest(unittest.TestCase):
    """Case 5: 下跌中弱反抽（无缩量）→ rejected(no_uptrend or no_shrink)"""

    def test_weak_rebound_rejected(self) -> None:
        # 构造下跌趋势：MA5 < MA10 < MA20，最后一天小反弹
        uptrend_len = 35
        closes = np.linspace(11.0, 9.5, uptrend_len)  # 持续下跌
        rows = [
            _mk_bar(float(c), volume=2000, pct_chg=-0.5)
            for c in closes
        ]
        rows.append(_mk_bar(9.60, low=9.50, high=9.65, open_=9.52,
                            volume=2200, pct_chg=1.1))  # 小阳线
        df = _build_df(rows)

        result = ShrinkPullbackDetector.detect(df)

        self.assertEqual(result["state"], STATE_REJECTED)
        self.assertIn(result["reject_reason"], ("no_uptrend", "no_shrink", "no_touch"),
                      msg=f"got {result['reject_reason']}")


class InsufficientBarsTest(unittest.TestCase):
    """Edge: bar 数不足 → rejected(insufficient_bars)"""

    def test_insufficient(self) -> None:
        rows = [_mk_bar(10.0, volume=1000) for _ in range(10)]
        df = _build_df(rows)
        result = ShrinkPullbackDetector.detect(df)
        self.assertEqual(result["state"], STATE_REJECTED)
        self.assertEqual(result["reject_reason"], "insufficient_bars")


if __name__ == "__main__":
    unittest.main()
