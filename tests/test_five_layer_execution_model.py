# -*- coding: utf-8 -*-
"""TDD RED phase: Tests for ExecutionModelResolver.

Three execution models: conservative, baseline, optimistic.
Handles limit-up/down blocks, gap adjustments, dual-trigger resolution.
"""

import unittest
from dataclasses import dataclass
from datetime import date
from typing import Optional

import pytest


@dataclass
class MockDailyBar:
    """Minimal daily bar for testing."""
    date: date
    open: float
    high: float
    low: float
    close: float
    pct_chg: float = 0.0


@pytest.mark.unit
class TestExecutionModelResolver(unittest.TestCase):
    """Test three-tier execution fill logic."""

    def _resolve(self, model, bars, signal_price=None, take_profit_pct=None,
                 stop_loss_pct=None):
        from src.backtest.execution.execution_model_resolver import ExecutionModelResolver
        return ExecutionModelResolver.resolve(
            execution_model=model,
            forward_bars=bars,
            signal_price=signal_price,
            take_profit_pct=take_profit_pct,
            stop_loss_pct=stop_loss_pct,
        )

    # ── Normal fills ──────────────────────────────────────────────────────

    def test_conservative_normal_fill_t1_open(self):
        """Conservative: entry at T+1 open."""
        bars = [
            MockDailyBar(date(2024, 1, 16), open=100.0, high=105.0, low=98.0, close=103.0),
        ]
        result = self._resolve("conservative", bars)
        self.assertEqual(result.fill_status, "filled")
        self.assertAlmostEqual(result.fill_price, 100.0)

    def test_baseline_normal_fill_vwap(self):
        """Baseline: entry at T+1 VWAP proxy ((H+L+C)/3)."""
        bars = [
            MockDailyBar(date(2024, 1, 16), open=100.0, high=106.0, low=97.0, close=103.0),
        ]
        result = self._resolve("baseline", bars)
        self.assertEqual(result.fill_status, "filled")
        expected_vwap = (106.0 + 97.0 + 103.0) / 3
        self.assertAlmostEqual(result.fill_price, expected_vwap, places=2)

    def test_optimistic_normal_fill_t1_open(self):
        """Optimistic: entry at T+1 open (favorable assumption)."""
        bars = [
            MockDailyBar(date(2024, 1, 16), open=100.0, high=108.0, low=99.0, close=105.0),
        ]
        result = self._resolve("optimistic", bars)
        self.assertEqual(result.fill_status, "filled")
        self.assertAlmostEqual(result.fill_price, 100.0)

    # ── 一字涨停买不到 (limit-up block) ──────────────────────────────────

    def test_conservative_limit_up_blocked(self):
        """Conservative: open==high && pct_chg>=9.9% → limit_blocked."""
        bars = [
            MockDailyBar(date(2024, 1, 16), open=110.0, high=110.0, low=110.0, close=110.0, pct_chg=10.0),
        ]
        result = self._resolve("conservative", bars)
        self.assertEqual(result.fill_status, "limit_blocked")
        self.assertTrue(result.limit_blocked)

    def test_baseline_limit_up_one_character_blocked(self):
        """Baseline: only blocks 一字涨停 (open==high==low==close)."""
        # 一字涨停 (open==high==low==close)
        bars_yizi = [
            MockDailyBar(date(2024, 1, 16), open=110.0, high=110.0, low=110.0, close=110.0, pct_chg=10.0),
        ]
        result = self._resolve("baseline", bars_yizi)
        self.assertEqual(result.fill_status, "limit_blocked")

    def test_baseline_limit_up_open_eq_high_not_blocked(self):
        """Baseline: open==high but low < open → can still fill (T字板)."""
        bars = [
            MockDailyBar(date(2024, 1, 16), open=110.0, high=110.0, low=105.0, close=109.0, pct_chg=10.0),
        ]
        result = self._resolve("baseline", bars)
        self.assertEqual(result.fill_status, "filled")

    def test_optimistic_limit_up_not_blocked(self):
        """Optimistic: assumes favorable entry, no limit-up block."""
        bars = [
            MockDailyBar(date(2024, 1, 16), open=110.0, high=110.0, low=110.0, close=110.0, pct_chg=10.0),
        ]
        result = self._resolve("optimistic", bars)
        self.assertEqual(result.fill_status, "filled")

    # ── 一字跌停卖不掉 (limit-down exit block) ───────────────────────────

    def test_conservative_limit_down_exit_blocked(self):
        """Conservative: open==low && pct_chg<=-9.9% → exit blocked."""
        entry_bar = MockDailyBar(date(2024, 1, 16), open=100.0, high=105.0, low=98.0, close=103.0)
        exit_bar = MockDailyBar(date(2024, 1, 17), open=90.0, high=90.0, low=90.0, close=90.0, pct_chg=-10.0)
        bars = [entry_bar, exit_bar]
        result = self._resolve("conservative", bars, stop_loss_pct=-3.0)
        # Should be filled (entry OK) but exit may be blocked
        self.assertEqual(result.fill_status, "filled")  # Entry fills
        self.assertTrue(result.exit_limit_blocked)

    # ── Gap scenarios ─────────────────────────────────────────────────────

    def test_gap_down_through_stop_loss(self):
        """Gap opens below stop → exit at gap open, gap_adjusted=True."""
        entry_bar = MockDailyBar(date(2024, 1, 16), open=100.0, high=105.0, low=98.0, close=103.0)
        gap_bar = MockDailyBar(date(2024, 1, 17), open=94.0, high=96.0, low=93.0, close=95.0)
        bars = [entry_bar, gap_bar]
        result = self._resolve("conservative", bars, stop_loss_pct=-3.0)
        self.assertEqual(result.fill_status, "filled")
        self.assertTrue(result.gap_adjusted)
        # Exit should be at the gap open, not the stop-loss price
        self.assertAlmostEqual(result.exit_fill_price, 94.0)

    def test_gap_up_through_take_profit(self):
        """Gap opens above take-profit → exit at gap open, gap_adjusted=True."""
        entry_bar = MockDailyBar(date(2024, 1, 16), open=100.0, high=105.0, low=98.0, close=103.0)
        gap_bar = MockDailyBar(date(2024, 1, 17), open=108.0, high=112.0, low=107.0, close=110.0)
        bars = [entry_bar, gap_bar]
        result = self._resolve("conservative", bars, take_profit_pct=5.0)
        self.assertEqual(result.fill_status, "filled")
        self.assertTrue(result.gap_adjusted)
        self.assertAlmostEqual(result.exit_fill_price, 108.0)

    # ── Dual trigger (same-day stop & take-profit) ────────────────────────

    def test_conservative_dual_trigger_stop_first(self):
        """Conservative: if both stop and take-profit triggered same day, stop wins."""
        entry_bar = MockDailyBar(date(2024, 1, 16), open=100.0, high=100.5, low=99.5, close=100.0)
        # Day where both stop (-3%) and take-profit (+5%) are hit
        dual_bar = MockDailyBar(date(2024, 1, 17), open=100.0, high=106.0, low=96.0, close=102.0)
        bars = [entry_bar, dual_bar]
        result = self._resolve("conservative", bars, stop_loss_pct=-3.0, take_profit_pct=5.0)
        self.assertEqual(result.fill_status, "filled")
        self.assertTrue(result.ambiguous_intraday_order)
        # Conservative assumes worst case: stop-loss hit first
        stop_price = 100.0 * (1 + (-3.0) / 100)
        self.assertAlmostEqual(result.exit_fill_price, stop_price, places=1)

    # ── No forward bars ───────────────────────────────────────────────────

    def test_no_forward_bars(self):
        """Should return pending status when no bars available."""
        result = self._resolve("conservative", [])
        self.assertEqual(result.fill_status, "pending")
        self.assertIsNone(result.fill_price)

    # ── Result structure ──────────────────────────────────────────────────

    def test_result_has_all_fields(self):
        """ExecutionResult should have all expected fields."""
        bars = [
            MockDailyBar(date(2024, 1, 16), open=100.0, high=105.0, low=98.0, close=103.0),
        ]
        result = self._resolve("conservative", bars)
        self.assertIsNotNone(result.fill_status)
        self.assertIsNotNone(result.fill_price)
        self.assertIsNotNone(result.fill_date)
        self.assertFalse(result.limit_blocked)
        self.assertFalse(result.gap_adjusted)
        self.assertFalse(result.ambiguous_intraday_order)


@pytest.mark.unit
class TestLimitPctResolver(unittest.TestCase):
    """B1: per-board limit-up / limit-down threshold resolution."""

    def test_main_board_default_10pct(self):
        from src.backtest.execution.limit_pct_resolver import resolve_limit_pct
        # 沪市主板
        self.assertEqual(resolve_limit_pct("600519"), (10.0, -10.0))
        # 深市主板
        self.assertEqual(resolve_limit_pct("000001"), (10.0, -10.0))
        # 中小板（已并入主板）
        self.assertEqual(resolve_limit_pct("002415"), (10.0, -10.0))

    def test_chinext_register_based_20pct(self):
        """创业板注册制后 ±20 %."""
        from src.backtest.execution.limit_pct_resolver import resolve_limit_pct
        self.assertEqual(resolve_limit_pct("300750"), (20.0, -20.0))  # 宁德时代
        self.assertEqual(resolve_limit_pct("301029"), (20.0, -20.0))

    def test_star_market_20pct(self):
        """科创板 ±20 %."""
        from src.backtest.execution.limit_pct_resolver import resolve_limit_pct
        self.assertEqual(resolve_limit_pct("688981"), (20.0, -20.0))  # 中芯国际
        self.assertEqual(resolve_limit_pct("689009"), (20.0, -20.0))

    def test_beijing_exchange_30pct_via_code_prefix(self):
        """北交所 ±30 %（按 code 前缀）."""
        from src.backtest.execution.limit_pct_resolver import resolve_limit_pct
        self.assertEqual(resolve_limit_pct("835174"), (30.0, -30.0))
        self.assertEqual(resolve_limit_pct("920002"), (30.0, -30.0))
        self.assertEqual(resolve_limit_pct("430510"), (30.0, -30.0))

    def test_beijing_exchange_30pct_via_exchange_field(self):
        """北交所 exchange 字段优先于 code 前缀."""
        from src.backtest.execution.limit_pct_resolver import resolve_limit_pct
        # Even with a non-BJSE-looking code, exchange="BJSE" wins
        self.assertEqual(
            resolve_limit_pct("123456", exchange="BJSE"),
            (30.0, -30.0),
        )

    def test_st_overrides_board_to_5pct(self):
        """ST/*ST → ±5 %, regardless of which board the stock lists on."""
        from src.backtest.execution.limit_pct_resolver import resolve_limit_pct
        self.assertEqual(resolve_limit_pct("600519", is_st=True), (5.0, -5.0))
        self.assertEqual(resolve_limit_pct("300750", is_st=True), (5.0, -5.0))
        self.assertEqual(resolve_limit_pct("688981", is_st=True), (5.0, -5.0))

    def test_non_cn_market_returns_none(self):
        """HK / US have no daily price limit → (None, None)."""
        from src.backtest.execution.limit_pct_resolver import resolve_limit_pct
        self.assertEqual(resolve_limit_pct("00700", market="hk"), (None, None))
        self.assertEqual(resolve_limit_pct("AAPL", market="us"), (None, None))

    def test_tolerance_helpers(self):
        """Helpers must match within the 0.1 % tolerance band."""
        from src.backtest.execution.limit_pct_resolver import (
            is_at_or_near_limit_down,
            is_at_or_near_limit_up,
        )
        # Within tolerance → True
        self.assertTrue(is_at_or_near_limit_up(9.95, 10.0))
        self.assertTrue(is_at_or_near_limit_up(19.95, 20.0))
        self.assertTrue(is_at_or_near_limit_up(29.92, 30.0))
        self.assertTrue(is_at_or_near_limit_up(4.95, 5.0))
        # Below tolerance → False
        self.assertFalse(is_at_or_near_limit_up(9.5, 10.0))
        self.assertFalse(is_at_or_near_limit_up(18.0, 20.0))
        # Mirror tests on the down side
        self.assertTrue(is_at_or_near_limit_down(-9.95, -10.0))
        self.assertTrue(is_at_or_near_limit_down(-19.92, -20.0))
        self.assertFalse(is_at_or_near_limit_down(-9.5, -10.0))
        # None inputs → False
        self.assertFalse(is_at_or_near_limit_up(None, 10.0))
        self.assertFalse(is_at_or_near_limit_up(9.95, None))


@pytest.mark.unit
class TestExecutionModelResolverPerBoardLimits(unittest.TestCase):
    """B1: ExecutionModelResolver must consume the per-board limit threshold
    so 创业板 +18 % (well below ±20 %) is not silently treated as
    limit-blocked, and 主板 +10 % is still treated as limit-blocked.
    """

    def _resolve(self, model, bars, **kwargs):
        from src.backtest.execution.execution_model_resolver import ExecutionModelResolver
        return ExecutionModelResolver.resolve(
            execution_model=model,
            forward_bars=bars,
            **kwargs,
        )

    def test_chinext_18pct_not_limit_blocked_under_conservative(self):
        """REGRESSION: 创业板 (300xxx) +18 % must not be limit-blocked.

        Pre-B1 the hard-coded ±9.9 % threshold meant 创业板/科创板 high-flyers
        were silently treated as 'limit-up, can't fill', which inflated
        ``limit_blocked`` rate and removed real entry samples from the
        analysis. After B1 the resolver must recognise 创业板 ±20 % and
        let +18 % bars through.
        """
        bar = MockDailyBar(
            date(2024, 1, 16), open=118.0, high=118.0, low=118.0,
            close=118.0, pct_chg=18.0,
        )
        result = self._resolve(
            "conservative", [bar], code="300750",
        )
        self.assertEqual(result.fill_status, "filled")
        self.assertFalse(result.limit_blocked)
        self.assertEqual(result.limit_up_pct, 20.0)

    def test_chinext_19_95pct_is_limit_blocked_under_conservative(self):
        """创业板 +19.95 % (within tolerance of ±20 %) → limit-blocked."""
        bar = MockDailyBar(
            date(2024, 1, 16), open=119.95, high=119.95, low=119.95,
            close=119.95, pct_chg=19.95,
        )
        result = self._resolve("conservative", [bar], code="300750")
        self.assertEqual(result.fill_status, "limit_blocked")
        self.assertTrue(result.limit_blocked)

    def test_star_market_19_97pct_is_limit_blocked(self):
        """科创板 (688xxx) +19.97 % → limit-blocked at ±20 %."""
        bar = MockDailyBar(
            date(2024, 1, 16), open=119.97, high=119.97, low=119.97,
            close=119.97, pct_chg=19.97,
        )
        result = self._resolve("conservative", [bar], code="688981")
        self.assertEqual(result.fill_status, "limit_blocked")
        self.assertEqual(result.limit_up_pct, 20.0)

    def test_beijing_exchange_29_5pct_not_limit_blocked(self):
        """北交所 (8xxxxx) +29.5 % is not yet at the ±30 % limit."""
        bar = MockDailyBar(
            date(2024, 1, 16), open=129.5, high=129.5, low=129.5,
            close=129.5, pct_chg=29.5,
        )
        result = self._resolve("conservative", [bar], code="835174")
        self.assertEqual(result.fill_status, "filled")
        self.assertEqual(result.limit_up_pct, 30.0)

    def test_beijing_exchange_29_92pct_is_limit_blocked(self):
        """北交所 +29.92 % within tolerance of ±30 % → limit-blocked."""
        bar = MockDailyBar(
            date(2024, 1, 16), open=129.92, high=129.92, low=129.92,
            close=129.92, pct_chg=29.92,
        )
        result = self._resolve("conservative", [bar], code="835174")
        self.assertEqual(result.fill_status, "limit_blocked")

    def test_st_4_95pct_is_limit_blocked(self):
        """ST/*ST +4.95 % within tolerance of ±5 % → limit-blocked."""
        bar = MockDailyBar(
            date(2024, 1, 16), open=104.95, high=104.95, low=104.95,
            close=104.95, pct_chg=4.95,
        )
        result = self._resolve("conservative", [bar], code="600519", is_st=True)
        self.assertEqual(result.fill_status, "limit_blocked")
        self.assertEqual(result.limit_up_pct, 5.0)

    def test_st_8pct_not_limit_blocked_on_pre_st_main_board_threshold(self):
        """ST/*ST +8 % is FAR above the ±5 % limit and should never be
        recorded for a real ST stock — but if it does happen (data quality
        issue), the resolver still flags it as limit_blocked because 8 %
        exceeds the ±5 % threshold.
        """
        bar = MockDailyBar(
            date(2024, 1, 16), open=108.0, high=108.0, low=108.0,
            close=108.0, pct_chg=8.0,
        )
        result = self._resolve("conservative", [bar], code="600519", is_st=True)
        self.assertEqual(result.fill_status, "limit_blocked")

    def test_main_board_10pct_still_limit_blocked(self):
        """REGRESSION GUARD: 主板 ±10 % behaviour unchanged after B1."""
        bar = MockDailyBar(
            date(2024, 1, 16), open=110.0, high=110.0, low=110.0,
            close=110.0, pct_chg=10.0,
        )
        result = self._resolve("conservative", [bar], code="600519")
        self.assertEqual(result.fill_status, "limit_blocked")
        self.assertEqual(result.limit_up_pct, 10.0)

    def test_legacy_call_without_code_falls_back_to_main_board(self):
        """Backwards compatibility: callers that don't pass code/is_st must
        still get the ±10 % default behaviour they had before B1.
        """
        bar = MockDailyBar(
            date(2024, 1, 16), open=110.0, high=110.0, low=110.0,
            close=110.0, pct_chg=10.0,
        )
        # No code / is_st / market kwargs — old signature
        result = self._resolve("conservative", [bar])
        self.assertEqual(result.fill_status, "limit_blocked")
        self.assertEqual(result.limit_up_pct, 10.0)

    def test_hk_market_does_not_block_on_pct_chg(self):
        """HK has no daily price limit → no limit_blocked regardless of pct_chg."""
        bar = MockDailyBar(
            date(2024, 1, 16), open=200.0, high=200.0, low=200.0,
            close=200.0, pct_chg=50.0,  # absurd for cn but possible in HK
        )
        result = self._resolve("conservative", [bar], code="00700", market="hk")
        self.assertEqual(result.fill_status, "filled")
        self.assertIsNone(result.limit_up_pct)


if __name__ == "__main__":
    unittest.main()
