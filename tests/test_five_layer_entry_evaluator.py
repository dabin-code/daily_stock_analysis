# -*- coding: utf-8 -*-
"""TDD RED phase: Tests for EntrySignalEvaluator.

Computes forward returns, MAE/MFE, drawdown, signal quality score.
"""

import unittest
from dataclasses import dataclass
from datetime import date

import pytest


@dataclass
class MockBar:
    date: date
    open: float
    high: float
    low: float
    close: float
    pct_chg: float = 0.0


@pytest.mark.unit
class TestEntrySignalEvaluator(unittest.TestCase):

    def _evaluate(self, entry_price, forward_bars, take_profit_pct=None,
                  stop_loss_pct=None):
        from src.backtest.evaluators.entry_evaluator import EntrySignalEvaluator
        return EntrySignalEvaluator.evaluate(
            entry_price=entry_price,
            forward_bars=forward_bars,
            take_profit_pct=take_profit_pct,
            stop_loss_pct=stop_loss_pct,
        )

    def test_forward_returns_basic(self):
        """Should compute 1d, 3d, 5d, 10d forward returns."""
        bars = [MockBar(date(2024, 1, d), 100+d, 102+d, 99+d, 101+d) for d in range(1, 12)]
        result = self._evaluate(100.0, bars)
        # 1d return = (close[0] - entry) / entry * 100
        self.assertIsNotNone(result.forward_return_1d)
        self.assertIsNotNone(result.forward_return_3d)
        self.assertIsNotNone(result.forward_return_5d)
        self.assertIsNotNone(result.forward_return_10d)

    def test_forward_return_1d_correct(self):
        """1-day return should be (close_day1 - entry) / entry * 100."""
        bars = [MockBar(date(2024, 1, 16), 101.0, 105.0, 99.0, 103.0)]
        result = self._evaluate(100.0, bars)
        self.assertAlmostEqual(result.forward_return_1d, 3.0)

    def test_mae_correct(self):
        """MAE = max adverse excursion (worst drawdown from entry)."""
        bars = [
            MockBar(date(2024, 1, 16), 100.0, 105.0, 95.0, 102.0),  # low=95
            MockBar(date(2024, 1, 17), 102.0, 108.0, 97.0, 106.0),  # low=97
        ]
        result = self._evaluate(100.0, bars)
        # MAE = (95 - 100) / 100 * 100 = -5.0
        self.assertAlmostEqual(result.mae, -5.0)

    def test_mfe_correct(self):
        """MFE = max favorable excursion (best runup from entry)."""
        bars = [
            MockBar(date(2024, 1, 16), 100.0, 105.0, 95.0, 102.0),  # high=105
            MockBar(date(2024, 1, 17), 102.0, 108.0, 97.0, 106.0),  # high=108
        ]
        result = self._evaluate(100.0, bars)
        # MFE = (108 - 100) / 100 * 100 = 8.0
        self.assertAlmostEqual(result.mfe, 8.0)

    def test_max_drawdown_from_peak(self):
        """Peak-to-trough drawdown during holding period."""
        bars = [
            MockBar(date(2024, 1, 16), 100.0, 110.0, 100.0, 108.0),  # peak=110
            MockBar(date(2024, 1, 17), 108.0, 109.0, 95.0, 96.0),    # trough=95
        ]
        result = self._evaluate(100.0, bars)
        # drawdown = (95 - 110) / 110 * 100 = -13.64%
        self.assertAlmostEqual(result.max_drawdown_from_peak, -13.64, places=1)

    def test_signal_quality_score(self):
        """signal_quality_score = MFE / (MFE + |MAE|), range 0-1."""
        bars = [
            MockBar(date(2024, 1, 16), 100.0, 106.0, 97.0, 104.0),
        ]
        result = self._evaluate(100.0, bars)
        # MFE=6.0, MAE=-3.0 → score = 6/(6+3) = 0.667
        expected = 6.0 / (6.0 + 3.0)
        self.assertAlmostEqual(result.signal_quality_score, expected, places=2)

    def test_plan_success_take_profit_hit(self):
        """plan_success=True when take-profit is hit."""
        bars = [
            MockBar(date(2024, 1, 16), 100.0, 100.5, 99.5, 100.0),
            MockBar(date(2024, 1, 17), 100.0, 106.0, 99.0, 105.0),
        ]
        result = self._evaluate(100.0, bars, take_profit_pct=5.0, stop_loss_pct=-3.0)
        self.assertTrue(result.plan_success)

    def test_plan_success_stop_loss_hit(self):
        """plan_success=False when stop-loss is hit first."""
        bars = [
            MockBar(date(2024, 1, 16), 100.0, 100.5, 99.5, 100.0),
            MockBar(date(2024, 1, 17), 100.0, 101.0, 96.0, 97.0),
        ]
        result = self._evaluate(100.0, bars, take_profit_pct=5.0, stop_loss_pct=-3.0)
        self.assertFalse(result.plan_success)

    def test_empty_bars(self):
        """Should return empty result for no bars."""
        result = self._evaluate(100.0, [])
        self.assertIsNone(result.forward_return_1d)

    def test_holding_days(self):
        """Should track holding period length as full window when no TP/SL."""
        bars = [MockBar(date(2024, 1, d), 100.0, 105.0, 95.0, 100.0) for d in range(16, 26)]
        result = self._evaluate(100.0, bars)
        self.assertEqual(result.holding_days, 10)

    def test_holding_days_with_tp_hit(self):
        """holding_days should be actual exit day when take-profit is hit."""
        bars = [
            MockBar(date(2024, 1, 16), 100.0, 100.5, 99.5, 100.0),
            MockBar(date(2024, 1, 17), 100.0, 106.0, 99.0, 105.0),  # TP hit at bar 1
            MockBar(date(2024, 1, 18), 105.0, 107.0, 104.0, 106.0),
        ]
        result = self._evaluate(100.0, bars, take_profit_pct=5.0, stop_loss_pct=-3.0)
        self.assertEqual(result.holding_days, 2)  # bar index 1 + 1

    def test_holding_days_with_sl_hit(self):
        """holding_days should be actual exit day when stop-loss is hit."""
        bars = [
            MockBar(date(2024, 1, 16), 100.0, 100.5, 99.5, 100.0),
            MockBar(date(2024, 1, 17), 100.0, 101.0, 96.0, 97.0),  # SL hit at bar 1
            MockBar(date(2024, 1, 18), 97.0, 98.0, 95.0, 96.0),
        ]
        result = self._evaluate(100.0, bars, take_profit_pct=5.0, stop_loss_pct=-3.0)
        self.assertEqual(result.holding_days, 2)  # bar index 1 + 1

    def test_holding_days_no_exit_trigger(self):
        """holding_days should be full window when neither TP nor SL is triggered."""
        bars = [
            MockBar(date(2024, 1, 16), 100.0, 101.0, 99.0, 100.5),
            MockBar(date(2024, 1, 17), 100.5, 102.0, 99.5, 101.0),
            MockBar(date(2024, 1, 18), 101.0, 103.0, 100.0, 102.0),
        ]
        result = self._evaluate(100.0, bars, take_profit_pct=5.0, stop_loss_pct=-3.0)
        self.assertEqual(result.holding_days, 3)

    def test_outcome_aligned_with_plan_success_win(self):
        """outcome should be 'win' when plan_success is True."""
        bars = [
            MockBar(date(2024, 1, 16), 100.0, 100.5, 99.5, 100.0),
            MockBar(date(2024, 1, 17), 100.0, 106.0, 99.0, 105.0),  # TP hit
        ]
        result = self._evaluate(100.0, bars, take_profit_pct=5.0, stop_loss_pct=-3.0)
        self.assertTrue(result.plan_success)
        self.assertEqual(result.outcome, "win")

    def test_outcome_aligned_with_plan_success_loss(self):
        """outcome should be 'loss' when plan_success is False."""
        bars = [
            MockBar(date(2024, 1, 16), 100.0, 100.5, 99.5, 100.0),
            MockBar(date(2024, 1, 17), 100.0, 101.0, 96.0, 97.0),  # SL hit
        ]
        result = self._evaluate(100.0, bars, take_profit_pct=5.0, stop_loss_pct=-3.0)
        self.assertFalse(result.plan_success)
        self.assertEqual(result.outcome, "loss")

    def test_outcome_fallback_to_5d_return_when_no_plan(self):
        """outcome should use forward_return_5d when no TP/SL params."""
        bars = [MockBar(date(2024, 1, d), 100+d, 102+d, 99+d, 101+d) for d in range(1, 12)]
        result = self._evaluate(100.0, bars)
        self.assertIsNone(result.plan_success)
        # forward_return_5d > 0 → win
        self.assertEqual(result.outcome, "win")

    def test_optimal_entry_deviation(self):
        """optimal_entry_deviation = (entry - min_low) / entry * 100."""
        bars = [
            MockBar(date(2024, 1, 16), 100.0, 105.0, 95.0, 102.0),  # low=95
            MockBar(date(2024, 1, 17), 102.0, 108.0, 97.0, 106.0),  # low=97
        ]
        result = self._evaluate(100.0, bars)
        # min_low=95, deviation = (100-95)/100*100 = 5.0
        self.assertAlmostEqual(result.optimal_entry_deviation, 5.0)

    def test_optimal_entry_deviation_perfect_entry(self):
        """Deviation should be 0 when entry price equals the window low."""
        bars = [
            MockBar(date(2024, 1, 16), 100.0, 105.0, 100.0, 103.0),  # low=100 = entry
            MockBar(date(2024, 1, 17), 103.0, 108.0, 101.0, 106.0),
        ]
        result = self._evaluate(100.0, bars)
        self.assertAlmostEqual(result.optimal_entry_deviation, 0.0)

    def test_optimal_entry_timing(self):
        """optimal_entry_timing should be 1-based day index of lowest price."""
        bars = [
            MockBar(date(2024, 1, 16), 100.0, 105.0, 98.0, 102.0),  # low=98
            MockBar(date(2024, 1, 17), 102.0, 108.0, 95.0, 106.0),  # low=95 ← min
            MockBar(date(2024, 1, 18), 106.0, 110.0, 99.0, 109.0),  # low=99
        ]
        result = self._evaluate(100.0, bars)
        self.assertEqual(result.optimal_entry_timing, 2)  # day 2 had lowest price

    def test_optimal_entry_timing_first_day(self):
        """optimal_entry_timing should be 1 when lowest price is on first day."""
        bars = [
            MockBar(date(2024, 1, 16), 100.0, 105.0, 93.0, 102.0),  # low=93 ← min
            MockBar(date(2024, 1, 17), 102.0, 108.0, 97.0, 106.0),
        ]
        result = self._evaluate(100.0, bars)
        self.assertEqual(result.optimal_entry_timing, 1)

    def test_outcome_is_none_when_window_immature_no_plan(self):
        """When forward_return_5d is None and no TP/SL plan is provided,
        outcome must be None (not 'loss').

        Regression guard for C1 in the backtest defect list: the previous
        ``(forward_return_5d or 0) > 0`` fallback silently labelled every
        immature sample as a loss, systematically dragging entry win-rates
        down and triggering false-positive recommendations.
        """
        bars = [
            MockBar(date(2024, 1, 16), 100.0, 101.0, 99.0, 100.5),
            MockBar(date(2024, 1, 17), 100.5, 102.0, 99.5, 101.0),
        ]
        result = self._evaluate(100.0, bars)

        self.assertIsNone(result.forward_return_5d)
        self.assertIsNone(result.plan_success)
        self.assertIsNone(result.outcome)

    def test_outcome_is_loss_when_5d_negative_no_plan(self):
        """forward_return_5d<0 with no plan should still produce 'loss'."""
        bars = [
            MockBar(date(2024, 1, 16), 100.0, 100.0, 95.0, 96.0),
            MockBar(date(2024, 1, 17), 96.0, 96.5, 92.0, 93.0),
            MockBar(date(2024, 1, 18), 93.0, 94.0, 90.0, 91.0),
            MockBar(date(2024, 1, 19), 91.0, 92.0, 88.0, 89.0),
            MockBar(date(2024, 1, 20), 89.0, 90.0, 86.0, 87.0),
        ]
        result = self._evaluate(100.0, bars)
        self.assertIsNotNone(result.forward_return_5d)
        self.assertLess(result.forward_return_5d, 0)
        self.assertEqual(result.outcome, "loss")
