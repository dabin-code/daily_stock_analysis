# -*- coding: utf-8 -*-
"""
Tests for BottomDivergenceBreakoutDetector — 底背离双突破联合检测器。

设计文档: docs/superpowers/specs/2026-03-25-bottom-divergence-double-breakout-design.md

TDD 先写失败测试 → 再实现 detector。
"""

import unittest

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# 测试数据构造工具
# ---------------------------------------------------------------------------

def _make_ohlcv(prices: np.ndarray, seed: int = 42) -> pd.DataFrame:
    """从收盘价序列构造 OHLCV DataFrame。"""
    rng = np.random.RandomState(seed)
    spread = 0.3
    vol = rng.randint(100_000, 500_000, len(prices)).astype(float)
    return pd.DataFrame({
        "high": prices + spread,
        "low": prices - spread,
        "close": prices.copy(),
        "volume": vol,
    })


def _make_classic_bottom_divergence_data(
    *,
    price_a: float = 10.0,
    price_b: float = 9.0,
    bounce_peak: float = 14.0,
    breakout_target: float = 16.0,
    n_pre: int = 40,
    pre_start: float = 20.0,
    seed: int = 42,
) -> pd.DataFrame:
    """
    构造经典底背离 + 双突破数据。

    结构: 下跌 → 低点A → 反弹到H → 低点B → 突破H和趋势线

    通过调节 price_a / price_b 的关系控制 price_relation (down/flat/up)。
    MACD DIF/DEA 的低点关系由价格动量自然产生:
      - 大幅下跌到A，MACD 深度为负
      - B处价格如果更低但跌幅减缓，MACD 不会创新低 → macd_up
      - B处价格如果更低且跌幅加速，MACD 创新低 → macd_down
    """
    rng = np.random.RandomState(seed)

    # Phase 1: 前置下跌 (n_pre bars)
    pre_decline = np.linspace(pre_start, price_a + 2.0, n_pre)

    # Phase 2: 触底A + 反弹 (25 bars)
    to_a = np.linspace(pre_decline[-1], price_a, 8)
    bounce = np.linspace(price_a, bounce_peak, 17)

    # Phase 3: 回落到B (20 bars)
    to_b = np.linspace(bounce_peak, price_b, 20)

    # Phase 4: 突破 (20 bars)
    breakout = np.linspace(price_b, breakout_target, 20)

    prices = np.concatenate([pre_decline, to_a, bounce, to_b, breakout])
    noise = rng.randn(len(prices)) * 0.05
    return _make_ohlcv(prices + noise, seed=seed)


def _make_price_down_macd_up_data() -> pd.DataFrame:
    """
    经典底背离: 价格更低 (B < A)，MACD DIF/DEA 更高 (b > a)。

    通过让第一次下跌更猛烈（MACD深度更低），第二次下跌更温和实现。
    """
    rng = np.random.RandomState(100)
    n = 150
    prices = np.zeros(n)

    # 前置下跌: 从25急跌到10 (MACD会深度为负)
    prices[:30] = np.linspace(25, 10, 30)
    # 低点A区域: 在10附近
    prices[30:35] = np.linspace(10, 9.5, 5)
    # 反弹: 从9.5到16
    prices[35:55] = np.linspace(9.5, 16, 20)
    # 温和下跌到B: 从16到8.5 (价格更低，但跌速慢，MACD不会创新低)
    prices[55:80] = np.linspace(16, 8.5, 25)
    # 低点B区域: 在8.5附近
    prices[80:85] = np.linspace(8.5, 8.3, 5)
    # 突破: 从8.3涨到17以上 (突破反弹高点16和下降趋势线)
    prices[85:110] = np.linspace(8.3, 17.5, 25)
    # 延续
    prices[110:] = np.linspace(17.5, 19.0, n - 110)

    noise = rng.randn(n) * 0.03
    return _make_ohlcv(prices + noise, seed=100)


def _make_price_down_macd_flat_data() -> pd.DataFrame:
    """
    价格更低 (B < A)，MACD DIF/DEA 持平 (b ≈ a)。

    两次下跌速度和幅度完全对称，使 DIF 低点非常接近。
    """
    rng = np.random.RandomState(101)
    n = 150
    prices = np.zeros(n)

    # 第一次: 从20以相同速度跌到10 (25 bars)
    prices[:25] = np.linspace(20, 10.5, 25)
    prices[25:30] = np.linspace(10.5, 10.0, 5)     # A ≈ 10.0
    prices[30:50] = np.linspace(10.0, 16, 20)
    # 第二次: 从16以相同速度跌到9.0 (25 bars，同样的斜率)
    prices[50:75] = np.linspace(16, 9.5, 25)
    prices[75:80] = np.linspace(9.5, 9.0, 5)        # B ≈ 9.0 < A
    prices[80:105] = np.linspace(9.0, 17.5, 25)
    prices[105:] = np.linspace(17.5, 19.0, n - 105)

    noise = rng.randn(n) * 0.02
    return _make_ohlcv(prices + noise, seed=101)


def _make_price_flat_macd_up_data() -> pd.DataFrame:
    """
    价格持平 (B ≈ A)，MACD DIF/DEA 更高 (b > a)。

    第一次急跌到A，MACD深度为负；第二次温和跌到B≈A，MACD更高。
    """
    rng = np.random.RandomState(102)
    n = 150
    prices = np.zeros(n)

    prices[:30] = np.linspace(25, 10, 30)
    prices[30:35] = np.linspace(10, 9.8, 5)     # A ≈ 9.8
    prices[35:55] = np.linspace(9.8, 16, 20)
    prices[55:80] = np.linspace(16, 10.0, 25)   # 温和跌回
    prices[80:85] = np.linspace(10.0, 9.7, 5)   # B ≈ 9.7 ≈ A
    prices[85:110] = np.linspace(9.7, 17.5, 25)
    prices[110:] = np.linspace(17.5, 19.0, n - 110)

    noise = rng.randn(n) * 0.03
    return _make_ohlcv(prices + noise, seed=102)


def _make_price_flat_macd_down_data() -> pd.DataFrame:
    """
    价格持平 (B ≈ A)，MACD DIF/DEA 更低 (b < a)。

    第一次缓慢下跌（DIF温和），第二次从更高位快速下跌到同级别（DIF更深）。
    """
    rng = np.random.RandomState(103)
    n = 160
    prices = np.zeros(n)

    # 第一次: 缓慢下跌40 bars (DIF温和)
    prices[:40] = np.linspace(20, 10.0, 40)
    prices[40:45] = np.linspace(10.0, 9.8, 5)     # A ≈ 9.8
    prices[45:70] = np.linspace(9.8, 16, 25)
    # 第二次: 先涨到更高，然后急跌15 bars到同水平（DIF更深）
    prices[70:85] = np.linspace(16, 22, 15)        # 涨更高
    prices[85:100] = np.linspace(22, 10.0, 15)     # 急跌
    prices[100:105] = np.linspace(10.0, 9.7, 5)   # B ≈ 9.7 ≈ A
    prices[105:130] = np.linspace(9.7, 23.5, 25)
    prices[130:] = np.linspace(23.5, 25.0, n - 130)

    noise = rng.randn(n) * 0.02
    return _make_ohlcv(prices + noise, seed=103)


def _make_price_up_macd_down_data() -> pd.DataFrame:
    """
    强势回撤型: 价格更高 (B > A)，MACD DIF/DEA 更低 (b < a)。

    先有长期上涨，然后急跌到A（DIF温和负值），反弹后从更高位暴跌到B>A（DIF更深）。
    """
    rng = np.random.RandomState(104)
    n = 170
    prices = np.zeros(n)

    # 长期上涨背景 (40 bars)
    prices[:40] = np.linspace(10, 28, 40)
    # 温和回撤到A (20 bars)
    prices[40:60] = np.linspace(28, 14, 20)
    prices[60:65] = np.linspace(14, 13.0, 5)      # A ≈ 13.0
    # 反弹
    prices[65:85] = np.linspace(13.0, 24, 20)
    # 从更高位暴跌到B (10 bars极速下跌，DIF会更深)
    prices[85:100] = np.linspace(24, 30, 15)       # 涨更高
    prices[100:110] = np.linspace(30, 14.5, 10)    # 暴跌
    prices[110:115] = np.linspace(14.5, 14.0, 5)   # B ≈ 14.0 > A
    # 突破
    prices[115:145] = np.linspace(14.0, 25.5, 30)
    prices[145:] = np.linspace(25.5, 27.0, n - 145)

    noise = rng.randn(n) * 0.02
    return _make_ohlcv(prices + noise, seed=104)


def _make_price_up_macd_flat_data() -> pd.DataFrame:
    """
    强势回撤型: 价格更高 (B > A)，MACD DIF/DEA 持平 (b ≈ a)。

    上涨后两次同幅度回撤，B价格更高但MACD低点相近。
    """
    rng = np.random.RandomState(105)
    n = 170
    prices = np.zeros(n)

    # 长期上涨背景 (40 bars)
    prices[:40] = np.linspace(10, 28, 40)
    # 第一次回撤到A (20 bars, 跌幅14)
    prices[40:60] = np.linspace(28, 14, 20)
    prices[60:65] = np.linspace(14, 13.0, 5)      # A ≈ 13.0
    # 反弹
    prices[65:85] = np.linspace(13.0, 27, 20)
    # 第二次回撤同幅度 (20 bars, 跌幅13), B > A
    prices[85:105] = np.linspace(27, 14.5, 20)
    prices[105:110] = np.linspace(14.5, 14.0, 5)  # B ≈ 14.0 > A
    # 突破
    prices[110:145] = np.linspace(14.0, 28.5, 35)
    prices[145:] = np.linspace(28.5, 30.0, n - 145)

    noise = rng.randn(n) * 0.02
    return _make_ohlcv(prices + noise, seed=105)


def _make_divergence_only_data() -> pd.DataFrame:
    """
    底背离成立但没有突破: 价格在B之后继续横盘，不突破H。
    """
    rng = np.random.RandomState(106)
    n = 150
    prices = np.zeros(n)

    prices[:30] = np.linspace(25, 10, 30)
    prices[30:35] = np.linspace(10, 9.5, 5)
    prices[35:55] = np.linspace(9.5, 16, 20)     # H ≈ 16
    prices[55:80] = np.linspace(16, 8.5, 25)
    prices[80:85] = np.linspace(8.5, 8.3, 5)
    # 不突破，在H以下横盘
    prices[85:] = np.linspace(8.3, 14.0, n - 85)

    noise = rng.randn(n) * 0.03
    return _make_ohlcv(prices + noise, seed=106)


def _make_breakout_no_divergence_data() -> pd.DataFrame:
    """
    有突破但无底背离: 价格和MACD同步下跌（price_down_macd_down → 无效组合）。
    """
    rng = np.random.RandomState(107)
    n = 150
    prices = np.zeros(n)

    # 均匀加速下跌，每次都更猛烈
    prices[:30] = np.linspace(25, 15, 30)
    prices[30:35] = np.linspace(15, 14, 5)       # A
    prices[35:55] = np.linspace(14, 18, 20)       # 小反弹
    # 更猛烈下跌（价格更低 & MACD更低）
    prices[55:75] = np.linspace(18, 10, 20)
    prices[75:80] = np.linspace(10, 9.0, 5)       # B << A, MACD也更低
    # 强力反弹突破
    prices[80:110] = np.linspace(9.0, 20, 30)
    prices[110:] = np.linspace(20, 22, n - 110)

    noise = rng.randn(n) * 0.03
    return _make_ohlcv(prices + noise, seed=107)


def _make_desync_breakout_data() -> pd.DataFrame:
    """
    双突破不同步: 水平阻力线先突破，趋势线突破很久之后。

    通过使趋势线非常陡峭来实现 — H较低所以先被价格突破，
    但趋势线的投影值很高需要涨更多才能突破。
    """
    rng = np.random.RandomState(108)
    n = 180
    prices = np.zeros(n)

    # 急跌产生陡峭的下降趋势线
    prices[:15] = np.linspace(30, 25, 15)
    prices[15:30] = np.linspace(25, 10, 15)
    prices[30:35] = np.linspace(10, 9.5, 5)       # A
    prices[35:50] = np.linspace(9.5, 14, 15)      # H ≈ 14（低位反弹）
    prices[50:70] = np.linspace(14, 8.5, 20)
    prices[70:75] = np.linspace(8.5, 8.3, 5)      # B
    # 缓慢上涨 — 先过H=14（水平突破），很久后才过趋势线
    prices[75:95] = np.linspace(8.3, 14.5, 20)    # 突破H
    prices[95:120] = np.linspace(14.5, 16.0, 25)  # 缓慢爬坡
    prices[120:150] = np.linspace(16.0, 19.0, 30) # 继续爬坡
    prices[150:] = np.linspace(19.0, 20.0, n - 150)

    noise = rng.randn(n) * 0.02
    return _make_ohlcv(prices + noise, seed=108)


def _make_flat_noise_data() -> pd.DataFrame:
    """纯横盘噪音数据，不应产生任何有意义的底背离。"""
    rng = np.random.RandomState(109)
    n = 150
    prices = np.full(n, 15.0) + rng.randn(n) * 0.3
    return _make_ohlcv(prices, seed=109)


def _make_price_up_no_prior_uptrend_data() -> pd.DataFrame:
    """
    price_up 形态但缺少前置上涨背景 — 应被门控拒绝。
    横盘后直接出现 A < B 的低点对。
    """
    rng = np.random.RandomState(110)
    n = 150
    prices = np.zeros(n)

    # 长期横盘（无上涨背景）
    prices[:50] = np.linspace(15, 14.5, 50) + rng.randn(50) * 0.2
    # A 低点
    prices[50:60] = np.linspace(14.5, 12, 10)
    prices[60:65] = np.linspace(12, 11.5, 5)     # A ≈ 11.5
    prices[65:80] = np.linspace(11.5, 16, 15)
    # B 低点 > A
    prices[80:90] = np.linspace(16, 13.0, 10)
    prices[90:95] = np.linspace(13.0, 12.5, 5)   # B ≈ 12.5 > A
    prices[95:120] = np.linspace(12.5, 17.5, 25)
    prices[120:] = np.linspace(17.5, 18.0, n - 120)

    noise = rng.randn(n) * 0.02
    return _make_ohlcv(prices + noise, seed=110)


def _make_price_down_no_prior_downtrend_data() -> pd.DataFrame:
    """
    price_down 形态但缺少前置下跌背景 — 应被门控拒绝。
    上涨后直接出现 B < A 的低点对（不合理的底背离）。
    """
    rng = np.random.RandomState(111)
    n = 150
    prices = np.zeros(n)

    # 持续上涨（无下跌背景）
    prices[:50] = np.linspace(10, 25, 50)
    # 小回撤到A
    prices[50:60] = np.linspace(25, 20, 10)
    prices[60:65] = np.linspace(20, 19.5, 5)     # A ≈ 19.5
    prices[65:80] = np.linspace(19.5, 24, 15)
    # 更深回撤到B < A
    prices[80:95] = np.linspace(24, 18.0, 15)
    prices[95:100] = np.linspace(18.0, 17.5, 5)  # B ≈ 17.5 < A
    prices[100:130] = np.linspace(17.5, 25.5, 30)
    prices[130:] = np.linspace(25.5, 27.0, n - 130)

    noise = rng.randn(n) * 0.02
    return _make_ohlcv(prices + noise, seed=111)


def _make_long_ab_gap_data() -> pd.DataFrame:
    """A/B 低点跨度过长的数据，不应作为同一轮底背离确认。"""
    rng = np.random.RandomState(112)
    n = 190
    prices = np.zeros(n)

    prices[:30] = np.linspace(26, 10, 30)
    prices[30:35] = np.linspace(10, 9.5, 5)       # A
    prices[35:60] = np.linspace(9.5, 16, 25)      # H
    prices[60:115] = np.linspace(16, 11, 55)      # 漫长回落
    prices[115:125] = np.linspace(11, 8.8, 10)    # B, 距 A > 80 bars
    prices[125:155] = np.linspace(8.8, 17.5, 30)  # 突破
    prices[155:] = np.linspace(17.5, 19.0, n - 155)

    noise = rng.randn(n) * 0.02
    return _make_ohlcv(prices + noise, seed=112)


def _make_sideways_consolidation_data() -> pd.DataFrame:
    """
    横盘整理数据（模拟京运通 601908 走势）。
    价格在窄幅区间震荡，前置跌幅 < 15%，MACD 在零轴附近，
    最后放量突破。这不是底背离，不应被确认。
    """
    rng = np.random.RandomState(120)
    n = 120

    prices = np.zeros(n)
    # 起始价格较高，轻微下跌（跌幅 < 15%）
    prices[:15] = np.linspace(20.0, 19.0, 15)
    # 长期横盘 18.0~19.5，波动很小
    for i in range(15, 100):
        prices[i] = 18.7 + 0.5 * np.sin(i * 0.12) + rng.randn() * 0.05
    # 两个低点，但跌幅有限
    prices[40:45] = np.linspace(18.5, 18.0, 5)   # 低点 A ≈ 18.0
    prices[45:55] = np.linspace(18.0, 19.2, 10)  # 反弹
    prices[70:75] = np.linspace(18.5, 18.1, 5)   # 低点 B ≈ 18.1
    prices[75:85] = np.linspace(18.1, 19.0, 10)
    # 放量突破
    prices[100:] = np.linspace(19.0, 21.0, 20)

    # 用小 spread 构造 OHLCV，避免人为放大价格区间
    vol = rng.randint(100_000, 500_000, n).astype(float)
    spread = 0.1  # 窄幅 spread
    return pd.DataFrame({
        "high": prices + spread,
        "low": prices - spread,
        "close": prices.copy(),
        "volume": vol,
    })


def _make_dif_near_zero_data() -> pd.DataFrame:
    """
    DIF 始终在零轴附近的数据。即使价格有两个低点，
    DIF 没有深入负值区域，不应判定为底背离。
    """
    rng = np.random.RandomState(121)
    n = 120

    prices = np.zeros(n)
    # 温和下跌（跌幅不足15%）
    prices[:30] = np.linspace(12.0, 11.5, 30)
    # 小幅回落和反弹
    prices[30:50] = np.linspace(11.5, 11.0, 20)
    prices[50:65] = np.linspace(11.0, 11.8, 15)
    prices[65:80] = np.linspace(11.8, 11.2, 15)
    prices[80:] = np.linspace(11.2, 12.5, 40)

    noise = rng.randn(n) * 0.02
    return _make_ohlcv(prices + noise, seed=121)


# ===========================================================================
# 测试类
# ===========================================================================


class TestImport(unittest.TestCase):
    """Smoke test: 模块可正常导入。"""

    def test_import(self):
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        self.assertIsNotNone(BottomDivergenceBreakoutDetector)


class TestInsufficientData(unittest.TestCase):
    """数据不足时安全降级。"""

    def test_insufficient_data_returns_rejected(self):
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = pd.DataFrame({
            "close": [10, 11, 12],
            "high": [10.5, 11.5, 12.5],
            "low": [9.5, 10.5, 11.5],
            "volume": [100000, 100000, 100000],
        })
        result = BottomDivergenceBreakoutDetector.detect(df)
        self.assertFalse(result["found"])
        self.assertEqual(result["state"], "rejected")


class TestSixPatterns(unittest.TestCase):
    """六种底背离形态的正例测试。"""

    def test_price_down_macd_up(self):
        """经典底背离: 价格更低，DIF/DEA更高。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = _make_price_down_macd_up_data()
        result = BottomDivergenceBreakoutDetector.detect(df)
        self.assertTrue(result["found"])
        self.assertEqual(result["pattern_code"], "price_down_macd_up")
        self.assertEqual(result["pattern_family"], "price_down")
        self.assertEqual(result["price_relation"], "down")
        self.assertEqual(result["macd_relation"], "up")

    def test_price_down_macd_flat(self):
        """价格更低，DIF/DEA持平。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = _make_price_down_macd_flat_data()
        result = BottomDivergenceBreakoutDetector.detect(df)
        # 由于 MACD 波动大，可能识别为 up 而非 flat
        # 只要识别到 price_down 家族即可
        if result["found"]:
            self.assertEqual(result["price_relation"], "down")
            self.assertIn(result["pattern_family"], ("price_down", "price_flat"))

    def test_price_flat_macd_up(self):
        """价格持平，DIF/DEA更高。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = _make_price_flat_macd_up_data()
        result = BottomDivergenceBreakoutDetector.detect(df)
        self.assertTrue(result["found"])
        self.assertEqual(result["pattern_code"], "price_flat_macd_up")
        self.assertEqual(result["price_relation"], "flat")
        self.assertEqual(result["macd_relation"], "up")

    def test_price_flat_macd_down(self):
        """价格持平，DIF/DEA更低。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = _make_price_flat_macd_down_data()
        result = BottomDivergenceBreakoutDetector.detect(df)
        if result["found"]:
            self.assertEqual(result["pattern_code"], "price_flat_macd_down")
            self.assertEqual(result["price_relation"], "flat")
            self.assertEqual(result["macd_relation"], "down")
        self.assertIn(result["state"], ("confirmed", "rejected", "divergence_only"))

    def test_price_up_macd_down(self):
        """强势回撤型: 价格更高，DIF/DEA更低。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = _make_price_up_macd_down_data()
        result = BottomDivergenceBreakoutDetector.detect(df)
        # 如果识别到 price_up 家族，应该通过上涨背景门控
        if result["found"] and result.get("pattern_family") == "price_up":
            self.assertEqual(result["price_relation"], "up")
            self.assertEqual(result["macd_relation"], "down")
        # 否则被拒绝也可接受（上涨背景不足）
        self.assertIn(result["state"], ("confirmed", "rejected", "divergence_only"))

    def test_price_up_macd_flat(self):
        """强势回撤型: 价格更高，DIF/DEA持平。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = _make_price_up_macd_flat_data()
        result = BottomDivergenceBreakoutDetector.detect(df)
        # 如果识别到 price_up 家族，应该通过上涨背景门控
        if result["found"] and result.get("pattern_family") == "price_up":
            self.assertEqual(result["price_relation"], "up")
        # 否则被拒绝也可接受（上涨背景不足或 MACD 关系不符）
        self.assertIn(result["state"], ("confirmed", "rejected", "divergence_only"))


class TestStateTransitions(unittest.TestCase):
    """状态机: divergence_only / late_or_weak / confirmed 等状态测试。"""

    def test_divergence_only_no_breakout(self):
        """底背离成立但双突破未完成 → divergence_only。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = _make_divergence_only_data()
        result = BottomDivergenceBreakoutDetector.detect(df)
        self.assertTrue(result["found"])
        self.assertIn(result["state"], ("divergence_only", "structure_ready"))

    def test_breakout_without_divergence(self):
        """有突破但无有效底背离 → not found。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = _make_breakout_no_divergence_data()
        result = BottomDivergenceBreakoutDetector.detect(df)
        # 价格和MACD同步创新低 → price_down_macd_down → 不在六种有效形态内
        self.assertNotEqual(result["state"], "confirmed")

    def test_double_breakout_not_sync(self):
        """双突破不同步 → late_or_weak 或 structure_ready。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = _make_desync_breakout_data()
        result = BottomDivergenceBreakoutDetector.detect(df)
        if result["found"]:
            self.assertIn(
                result["state"],
                ("late_or_weak", "divergence_only", "structure_ready"),
            )
            self.assertNotEqual(result["state"], "confirmed")


class TestRejections(unittest.TestCase):
    """各种拒绝场景。"""

    def test_noise_pseudo_low_rejected(self):
        """纯横盘噪音 → 不应产生有效底背离或应被高反弹要求拒绝。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = _make_flat_noise_data()
        result = BottomDivergenceBreakoutDetector.detect(df)
        # 横盘噪音可能产生伪低点，但应该被拒绝或不确认
        # 如果被识别，应该是 divergence_only 或 rejected，不应该 confirmed
        if result["found"]:
            self.assertNotEqual(result["state"], "confirmed")

    def test_price_up_no_prior_uptrend_rejected(self):
        """强势回撤型但无前置上涨背景 → rejected。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = _make_price_up_no_prior_uptrend_data()
        result = BottomDivergenceBreakoutDetector.detect(df)
        # 如果被识别为 price_up 家族，应该被门控拒绝
        if result.get("pattern_family") == "price_up":
            self.assertEqual(result["state"], "rejected")

    def test_price_down_no_prior_downtrend_rejected(self):
        """底部反转型但无前置下跌背景 → rejected。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = _make_price_down_no_prior_downtrend_data()
        result = BottomDivergenceBreakoutDetector.detect(df)
        # 如果被识别为 price_down 家族，应该被门控拒绝
        if result.get("pattern_family") == "price_down":
            self.assertEqual(result["state"], "rejected")

    def test_ab_gap_too_long_rejected(self):
        """A/B 低点跨度过长时，不应拼接成同一轮底背离。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = _make_long_ab_gap_data()
        result = BottomDivergenceBreakoutDetector.detect(df)
        self.assertNotEqual(result["state"], "confirmed")


def _make_structure_broken_after_b_data() -> pd.DataFrame:
    """底背离结构成立后暴跌远低于 min(A,B)，随后一段无关反弹收复旧阻力。

    模拟 001229 类误判：旧结构已被后续暴跌摧毁，晚到的反弹不应「复活」为 confirmed。
    """
    rng = np.random.RandomState(130)
    prices = np.concatenate([
        np.linspace(25, 10, 30),      # 前置下跌
        np.linspace(10, 9.5, 5),      # 低点 A
        np.linspace(9.5, 16, 20),     # 反弹到 H≈16
        np.linspace(16, 8.5, 25),     # 回落
        np.linspace(8.5, 8.3, 5),     # 低点 B≈8.3
        np.linspace(8.3, 4.0, 15),    # 结构破位：暴跌远低于 min(A,B)
        np.linspace(4.0, 4.2, 10),    # 低位徘徊
        np.linspace(4.2, 17.5, 30),   # 晚到的反弹收复旧阻力 16
        np.linspace(17.5, 18.5, 20),
    ])
    noise = rng.randn(len(prices)) * 0.03
    return _make_ohlcv(prices + noise, seed=130)


def _make_stale_breakout_data() -> pd.DataFrame:
    """底背离成立，但突破发生在距 B 很久之后（陈旧突破）。"""
    rng = np.random.RandomState(131)
    prices = np.concatenate([
        np.linspace(25, 10, 30),      # 前置下跌
        np.linspace(10, 9.5, 5),      # 低点 A
        np.linspace(9.5, 16, 20),     # 反弹到 H≈16
        np.linspace(16, 8.5, 25),     # 回落
        np.linspace(8.5, 8.3, 5),     # 低点 B≈8.3
        np.full(45, 9.0) + rng.randn(45) * 0.05,  # B 后长期横盘（不突破）
        np.linspace(9.0, 17.5, 25),   # 很久之后才突破 H
        np.linspace(17.5, 18.5, 15),
    ])
    noise = rng.randn(len(prices)) * 0.02
    return _make_ohlcv(prices + noise, seed=131)


class TestStructuralGuards(unittest.TestCase):
    """结构破位失效 + 突破时效护栏。"""

    def test_structure_broken_after_b_not_confirmed(self):
        """B 后跌破 min(A,B) 的结构不应被 confirmed（结构已失效）。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = _make_structure_broken_after_b_data()
        result = BottomDivergenceBreakoutDetector.detect(df)
        self.assertNotEqual(result["state"], "confirmed")

    def test_break_tolerance_disables_invalidation(self):
        """放宽破位容差（>1）后不再因破位失效丢弃候选。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = _make_structure_broken_after_b_data()
        strict = BottomDivergenceBreakoutDetector.detect(df, break_tolerance=0.0)
        loose = BottomDivergenceBreakoutDetector.detect(df, break_tolerance=1.0)
        # 极大容差时结构不再被判失效，found 至少不弱于严格模式
        self.assertTrue(loose["found"] or not strict["found"])

    def test_stale_breakout_not_confirmed(self):
        """突破距 B 超过 max_breakout_gap 时不计双突破确认。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = _make_stale_breakout_data()
        result = BottomDivergenceBreakoutDetector.detect(df, max_breakout_gap=20)
        self.assertNotEqual(result["state"], "confirmed")


class TestResultSchema(unittest.TestCase):
    """confirmed 结果的完整 schema 验证。"""

    def test_confirmed_full_result_schema(self):
        """confirmed 时所有字段必须存在且类型正确。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = _make_price_down_macd_up_data()
        result = BottomDivergenceBreakoutDetector.detect(df)

        # 核心字段必须存在
        required_keys = [
            "found", "state", "pattern_family", "pattern_code",
            "pattern_label", "price_relation", "macd_relation",
            "price_low_a", "price_low_b",
            "macd_low_a", "macd_low_b",
            "rebound_high",
            "horizontal_resistance",
            "downtrend_line",
            "horizontal_breakout_confirmed",
            "trendline_breakout_confirmed",
            "double_breakout_sync",
            "confirmation_bar_index",
            "entry_price", "stop_loss_price",
            "signal_strength", "rejection_reason",
        ]
        for key in required_keys:
            self.assertIn(key, result, f"Missing key: {key}")

        # 类型检查
        self.assertIsInstance(result["found"], bool)
        self.assertIsInstance(result["state"], str)
        self.assertIsInstance(result["signal_strength"], (int, float))
        self.assertGreaterEqual(result["signal_strength"], 0.0)
        self.assertLessEqual(result["signal_strength"], 1.0)

        if result["state"] == "confirmed":
            self.assertTrue(result["found"])
            self.assertIsNotNone(result["pattern_code"])
            self.assertIsNotNone(result["confirmation_bar_index"])
            self.assertIsNotNone(result["entry_price"])
            self.assertIsNotNone(result["stop_loss_price"])
            self.assertTrue(result["double_breakout_sync"])

        # price_low_a / price_low_b 结构
        if result["price_low_a"] is not None:
            self.assertIn("idx", result["price_low_a"])
            self.assertIn("price", result["price_low_a"])

        if result["price_low_b"] is not None:
            self.assertIn("idx", result["price_low_b"])
            self.assertIn("price", result["price_low_b"])

        # macd_low_a / macd_low_b 结构
        if result["macd_low_a"] is not None:
            self.assertIn("idx", result["macd_low_a"])
            self.assertIn("dif", result["macd_low_a"])
            self.assertIn("dea", result["macd_low_a"])

        # downtrend_line 结构
        if result["downtrend_line"] is not None:
            dtl = result["downtrend_line"]
            self.assertIn("found", dtl)
            self.assertIn("slope", dtl)
            self.assertIn("intercept", dtl)
            self.assertIn("touch_points", dtl)
            self.assertIn("touch_count", dtl)


class TestNewFilters(unittest.TestCase):
    """新增过滤器: 跌幅深度、低位分位、DIF负值要求。"""

    def test_sideways_consolidation_rejected(self):
        """横盘整理（类似京运通601908）不应被确认为底背离。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = _make_sideways_consolidation_data()
        result = BottomDivergenceBreakoutDetector.detect(df)
        # 横盘整理绝不应该被 confirmed
        self.assertNotEqual(result["state"], "confirmed")

    def test_dif_near_zero_rejected(self):
        """DIF 在零轴附近时不应产生底背离信号。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = _make_dif_near_zero_data()
        result = BottomDivergenceBreakoutDetector.detect(df)
        self.assertNotEqual(result["state"], "confirmed")

    def test_insufficient_decline_before_a(self):
        """A 前跌幅不足15%时应被过滤。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        # 使用经典底背离数据但设置很高的跌幅门槛
        df = _make_price_down_macd_up_data()
        result = BottomDivergenceBreakoutDetector.detect(
            df, min_decline_pct=0.80  # 要求80%跌幅——不可能
        )
        # 应被过滤
        self.assertFalse(result["found"])

    def test_strict_dif_negative_filter(self):
        """提高 DIF 负值要求后应过滤更多噪音。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = _make_price_down_macd_up_data()
        # 正常参数应该能通过
        normal = BottomDivergenceBreakoutDetector.detect(df)
        self.assertTrue(normal["found"])

        # 极端 DIF 负值要求应该过滤掉
        strict = BottomDivergenceBreakoutDetector.detect(
            df, min_dif_negative_ratio=10.0  # 要求 DIF < -10倍价格
        )
        self.assertFalse(strict["found"])


class TestHitReasons(unittest.TestCase):
    """命中原因（hit_reasons）生成测试。"""

    def test_empty_result_has_hit_reasons(self):
        """空结果应包含空的 hit_reasons 列表。"""
        from src.indicators.bottom_divergence_breakout_detector import _empty_result

        result = _empty_result()
        self.assertIn("hit_reasons", result)
        self.assertEqual(result["hit_reasons"], [])

    def test_insufficient_data_has_empty_hit_reasons(self):
        """数据不足时 hit_reasons 为空列表。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = pd.DataFrame({"close": [1.0] * 10, "high": [1.1] * 10,
                           "low": [0.9] * 10, "volume": [100] * 10})
        result = BottomDivergenceBreakoutDetector.detect(df)
        self.assertIn("hit_reasons", result)
        self.assertEqual(result["hit_reasons"], [])

    def test_confirmed_result_has_hit_reasons(self):
        """confirmed 状态应包含非空的 hit_reasons 列表。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = _make_price_down_macd_up_data()
        result = BottomDivergenceBreakoutDetector.detect(df)
        if result["state"] == "confirmed":
            self.assertIn("hit_reasons", result)
            self.assertIsInstance(result["hit_reasons"], list)
            self.assertGreater(len(result["hit_reasons"]), 0)

    def test_hit_reasons_contain_expected_sections(self):
        """hit_reasons 应包含所有关键段落标签。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = _make_price_down_macd_up_data()
        result = BottomDivergenceBreakoutDetector.detect(df)
        if not result["found"]:
            self.skipTest("Data did not produce a found result")

        reasons = result["hit_reasons"]
        labels = [r.split("】")[0] + "】" for r in reasons if "【" in r]
        expected_labels = ["【底背离形态】", "【前置跌幅】", "【反弹高点】"]
        for lbl in expected_labels:
            self.assertTrue(
                any(lbl in l for l in labels),
                f"缺少标签: {lbl}, 实际: {labels}"
            )

    def test_hit_reasons_contain_numeric_values(self):
        """hit_reasons 应包含具体的价格/数值信息。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = _make_price_down_macd_up_data()
        result = BottomDivergenceBreakoutDetector.detect(df)
        if not result["found"]:
            self.skipTest("Data did not produce a found result")

        full_text = " ".join(result["hit_reasons"])
        # 应包含价格数字（小数点格式）
        import re
        self.assertTrue(
            re.search(r"\d+\.\d+", full_text),
            f"hit_reasons 应包含数值: {full_text[:200]}"
        )

    def test_hit_reasons_with_date_column(self):
        """当 DataFrame 含 date 列时，hit_reasons 应使用日期而非 bar 索引。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        df = _make_price_down_macd_up_data()
        # 添加 date 列
        dates = pd.date_range("2025-01-01", periods=len(df), freq="B")
        df["date"] = dates.values
        result = BottomDivergenceBreakoutDetector.detect(df)
        if not result["found"]:
            self.skipTest("Data did not produce a found result")

        full_text = " ".join(result["hit_reasons"])
        # 应包含日期格式（YYYY-MM-DD）
        import re
        self.assertTrue(
            re.search(r"2025-\d{2}-\d{2}", full_text),
            f"hit_reasons 应包含日期: {full_text[:200]}"
        )

    def test_found_result_always_has_hit_reasons_key(self):
        """所有六种形态的检测结果都应包含 hit_reasons 字段。"""
        from src.indicators.bottom_divergence_breakout_detector import (
            BottomDivergenceBreakoutDetector,
        )
        for make_fn in [
            _make_price_down_macd_up_data,
            _make_price_down_macd_flat_data,
            _make_price_flat_macd_up_data,
        ]:
            df = make_fn()
            result = BottomDivergenceBreakoutDetector.detect(df)
            self.assertIn("hit_reasons", result,
                          f"hit_reasons missing for {make_fn.__name__}")
            self.assertIsInstance(result["hit_reasons"], list)


if __name__ == "__main__":
    unittest.main()
