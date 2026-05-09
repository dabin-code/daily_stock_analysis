# -*- coding: utf-8 -*-
"""B5: tests for ``StockRepository.get_forward_bars_with_meta``.

Covers:
  * Healthy 5-day window (5 trading days, ~7 calendar days span) — neither
    gap_too_long nor insufficient_bars triggers.
  * Suspended-trading sample — 5 bars actually span 40+ calendar days
    because the stock was halted; gap_too_long must trigger.
  * Under-resourced window — only 2 bars come back for a 5d ask;
    insufficient_bars must trigger (and gap_too_long must NOT, because
    actual_span_days is small).
  * Empty window — actual_bar_count=0, actual_span_days=None.
  * Custom tolerance_factor — gap_threshold_days scales with the factor.
  * Backwards-compat ``get_forward_bars`` facade still returns just the bar
    list, dropping the metadata.
"""

import os
import tempfile
import unittest
from datetime import date, timedelta

import pytest


@pytest.mark.unit
class TestForwardBarsWithMeta(unittest.TestCase):

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test_b5_repo.db")
        os.environ["DATABASE_PATH"] = self._db_path
        from src.config import Config
        Config._instance = None
        from src.storage import DatabaseManager
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()

    def tearDown(self):
        from src.storage import DatabaseManager
        from src.config import Config
        DatabaseManager.reset_instance()
        Config._instance = None
        os.environ.pop("DATABASE_PATH", None)
        self._temp_dir.cleanup()

    def _seed_bars(self, code, dates):
        from src.storage import StockDaily
        with self.db.get_session() as session:
            for d in dates:
                session.add(StockDaily(
                    code=code, date=d,
                    open=100.0, high=101.0, low=99.0, close=100.5,
                    pct_chg=0.5, volume=1000.0, amount=100000.0,
                ))
            session.commit()

    def _repo(self):
        from src.repositories.stock_repo import StockRepository
        return StockRepository(self.db)

    def test_healthy_5_day_window_does_not_trigger_either_flag(self):
        """5 consecutive trading days (~Mon-Fri) → span ~4 days, neither
        flag triggers; analyst can rely on forward_return_5d.
        """
        self._seed_bars("600519", [date(2024, 1, 16) + timedelta(days=i) for i in range(5)])
        bars, meta = self._repo().get_forward_bars_with_meta(
            code="600519", analysis_date=date(2024, 1, 15), eval_window_days=5,
        )
        self.assertEqual(len(bars), 5)
        self.assertEqual(meta.actual_bar_count, 5)
        self.assertEqual(meta.actual_span_days, 4)
        self.assertFalse(meta.gap_too_long)
        self.assertFalse(meta.insufficient_bars)
        self.assertEqual(meta.gap_threshold_days, 10)  # ceil(5 * 2.0)

    def test_5_day_window_with_weekend_does_not_trigger_gap(self):
        """A Mon-Fri-Mon-Tue-Wed pattern (covers 1 weekend) spans ~9 days,
        which is still under the default tolerance (10 calendar days).
        """
        # Skip Sat/Sun (Jan 20 is Sat, Jan 21 is Sun in 2024)
        self._seed_bars(
            "000001",
            [date(2024, 1, 16), date(2024, 1, 17), date(2024, 1, 18),
             date(2024, 1, 19), date(2024, 1, 22)],
        )
        bars, meta = self._repo().get_forward_bars_with_meta(
            code="000001", analysis_date=date(2024, 1, 15), eval_window_days=5,
        )
        self.assertEqual(len(bars), 5)
        self.assertEqual(meta.actual_span_days, 6)
        self.assertFalse(meta.gap_too_long)

    def test_suspension_long_gap_triggers_gap_too_long(self):
        """5 bars actually span 60 calendar days (typical of stock halted
        for ~2 months mid-window) → gap_too_long must trigger so the
        forward_return_5d denominator is treated as untrustworthy.
        """
        self._seed_bars(
            "000333",
            [date(2024, 1, 16), date(2024, 1, 17), date(2024, 3, 12),
             date(2024, 3, 13), date(2024, 3, 14)],
        )
        bars, meta = self._repo().get_forward_bars_with_meta(
            code="000333", analysis_date=date(2024, 1, 15), eval_window_days=5,
        )
        self.assertEqual(meta.actual_bar_count, 5)
        self.assertGreater(meta.actual_span_days, meta.gap_threshold_days)
        self.assertTrue(meta.gap_too_long)
        self.assertFalse(meta.insufficient_bars)

    def test_underresourced_window_triggers_insufficient_bars(self):
        """Only 2 bars come back for a 5d ask (e.g. listing got delisted
        mid-window or data sync gap) → insufficient_bars triggers.
        gap_too_long must NOT trigger because the small bar set spans
        only a few days.
        """
        self._seed_bars("000002", [date(2024, 1, 16), date(2024, 1, 17)])
        bars, meta = self._repo().get_forward_bars_with_meta(
            code="000002", analysis_date=date(2024, 1, 15), eval_window_days=5,
        )
        self.assertEqual(meta.actual_bar_count, 2)
        self.assertEqual(meta.actual_span_days, 1)
        self.assertFalse(meta.gap_too_long)
        # ceil(5 * 0.6) = 3, so 2 bars is insufficient
        self.assertTrue(meta.insufficient_bars)

    def test_empty_window_returns_no_bars_and_no_span(self):
        """No data at all → actual_bar_count=0, span=None, both flags
        coherent (gap_too_long needs span info, insufficient_bars True).
        """
        bars, meta = self._repo().get_forward_bars_with_meta(
            code="ZZZZZZ", analysis_date=date(2024, 1, 15), eval_window_days=5,
        )
        self.assertEqual(len(bars), 0)
        self.assertEqual(meta.actual_bar_count, 0)
        self.assertIsNone(meta.actual_span_days)
        self.assertFalse(meta.gap_too_long)
        self.assertTrue(meta.insufficient_bars)

    def test_single_bar_window_has_none_span_but_not_gap(self):
        """Exactly 1 bar back → span undefined; insufficient_bars True
        but gap_too_long must stay False (no two timestamps to diff).
        """
        self._seed_bars("000003", [date(2024, 1, 16)])
        _, meta = self._repo().get_forward_bars_with_meta(
            code="000003", analysis_date=date(2024, 1, 15), eval_window_days=5,
        )
        self.assertEqual(meta.actual_bar_count, 1)
        self.assertIsNone(meta.actual_span_days)
        self.assertFalse(meta.gap_too_long)
        self.assertTrue(meta.insufficient_bars)

    def test_custom_tolerance_factor_scales_threshold(self):
        """Setting tolerance_factor=1.2 on a 5d window puts the cutoff at
        ceil(5*1.2)=6 days, which is tighter than the default and would
        flag a sample whose 5 bars span 7 calendar days.
        """
        self._seed_bars(
            "000004",
            [date(2024, 1, 16), date(2024, 1, 17), date(2024, 1, 18),
             date(2024, 1, 19), date(2024, 1, 23)],  # spans 7 days
        )
        _, meta = self._repo().get_forward_bars_with_meta(
            code="000004", analysis_date=date(2024, 1, 15),
            eval_window_days=5, tolerance_factor=1.2,
        )
        self.assertEqual(meta.gap_threshold_days, 6)
        self.assertEqual(meta.actual_span_days, 7)
        self.assertTrue(meta.gap_too_long)

    def test_legacy_get_forward_bars_facade_returns_only_bars(self):
        """REGRESSION GUARD: callers that haven't upgraded to with_meta
        must still receive a plain ``List[StockDaily]``.
        """
        self._seed_bars("000005", [date(2024, 1, 16) + timedelta(days=i) for i in range(3)])
        bars = self._repo().get_forward_bars(
            code="000005", analysis_date=date(2024, 1, 15), eval_window_days=5,
        )
        self.assertIsInstance(bars, list)
        self.assertEqual(len(bars), 3)


@pytest.mark.unit
class TestForwardQualityHelpers(unittest.TestCase):
    """Tests for the standalone helpers used by ``_process_candidate``."""

    def test_resolve_quality_reason_prioritises_no_forward_bars(self):
        from src.repositories.stock_repo import ForwardBarsMeta
        from src.backtest.services.backtest_service import _resolve_forward_quality_reason

        meta = ForwardBarsMeta(
            requested_window_days=5, actual_bar_count=0, actual_span_days=None,
            gap_threshold_days=10, gap_too_long=False, insufficient_bars=True,
            tolerance_factor=2.0,
        )
        self.assertEqual(_resolve_forward_quality_reason(meta), "no_forward_bars")

    def test_resolve_quality_reason_prioritises_gap_over_insufficient(self):
        """A halted stock often returns 0-1 bars (which would also satisfy
        insufficient_bars); attributing the suppression to gap_too_long is
        more actionable than the generic "not enough bars" code.
        """
        from src.repositories.stock_repo import ForwardBarsMeta
        from src.backtest.services.backtest_service import _resolve_forward_quality_reason

        meta = ForwardBarsMeta(
            requested_window_days=5, actual_bar_count=2, actual_span_days=60,
            gap_threshold_days=10, gap_too_long=True, insufficient_bars=True,
            tolerance_factor=2.0,
        )
        self.assertEqual(_resolve_forward_quality_reason(meta), "gap_too_long")

    def test_resolve_quality_reason_returns_none_for_healthy_window(self):
        from src.repositories.stock_repo import ForwardBarsMeta
        from src.backtest.services.backtest_service import _resolve_forward_quality_reason

        meta = ForwardBarsMeta(
            requested_window_days=5, actual_bar_count=5, actual_span_days=4,
            gap_threshold_days=10, gap_too_long=False, insufficient_bars=False,
            tolerance_factor=2.0,
        )
        self.assertIsNone(_resolve_forward_quality_reason(meta))

    def test_gap_check_enabled_default_true(self):
        from src.backtest.services.backtest_service import _is_forward_gap_check_enabled

        os.environ.pop("BACKTEST_FORWARD_GAP_CHECK_ENABLED", None)
        try:
            self.assertTrue(_is_forward_gap_check_enabled())
        finally:
            os.environ.pop("BACKTEST_FORWARD_GAP_CHECK_ENABLED", None)

    def test_gap_check_can_be_disabled_via_env(self):
        from src.backtest.services.backtest_service import _is_forward_gap_check_enabled

        for value in ("false", "0", "FALSE", "No", "off"):
            os.environ["BACKTEST_FORWARD_GAP_CHECK_ENABLED"] = value
            try:
                self.assertFalse(
                    _is_forward_gap_check_enabled(),
                    f"value={value!r} should disable the gap check",
                )
            finally:
                os.environ.pop("BACKTEST_FORWARD_GAP_CHECK_ENABLED", None)

    def test_tolerance_factor_default_is_2(self):
        from src.backtest.services.backtest_service import _get_forward_gap_tolerance_factor
        from src.repositories.stock_repo import DEFAULT_GAP_TOLERANCE_FACTOR

        os.environ.pop("BACKTEST_FORWARD_GAP_TOLERANCE_FACTOR", None)
        self.assertEqual(_get_forward_gap_tolerance_factor(), DEFAULT_GAP_TOLERANCE_FACTOR)

    def test_tolerance_factor_can_be_overridden_via_env(self):
        from src.backtest.services.backtest_service import _get_forward_gap_tolerance_factor

        os.environ["BACKTEST_FORWARD_GAP_TOLERANCE_FACTOR"] = "1.5"
        try:
            self.assertEqual(_get_forward_gap_tolerance_factor(), 1.5)
        finally:
            os.environ.pop("BACKTEST_FORWARD_GAP_TOLERANCE_FACTOR", None)

    def test_tolerance_factor_invalid_value_falls_back_to_default(self):
        from src.backtest.services.backtest_service import _get_forward_gap_tolerance_factor
        from src.repositories.stock_repo import DEFAULT_GAP_TOLERANCE_FACTOR

        for value in ("abc", "-1", "0", ""):
            os.environ["BACKTEST_FORWARD_GAP_TOLERANCE_FACTOR"] = value
            try:
                # Empty string falls back via the "if not raw" branch;
                # other invalids via the except branch.
                self.assertEqual(
                    _get_forward_gap_tolerance_factor(),
                    DEFAULT_GAP_TOLERANCE_FACTOR,
                )
            finally:
                os.environ.pop("BACKTEST_FORWARD_GAP_TOLERANCE_FACTOR", None)


if __name__ == "__main__":
    unittest.main()
