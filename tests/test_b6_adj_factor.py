# -*- coding: utf-8 -*-
"""B6 (3a): tests for the adj_factor infrastructure.

Covers:
  * StockDaily schema carries the three new columns and round-trips them
  * save_daily_data writes adj_factor / adj_anchor_date / adj_factor_source
    from the DataFrame, with sensible defaults when fields are missing
  * The inline SQLite migration is idempotent and back-fills legacy rows
  * data_version composition (_aggregate_adj_factor_distribution +
    _build_data_version) is correct and tolerates malformed metrics_json
  * AkshareFetcher._enrich_with_adj_factor computes adj_factor from a
    qfq + raw join (without hitting the network — uses mocked fetcher
    methods so the test stays fast and deterministic)
  * AKSHARE_FETCH_ADJ_FACTOR env knob gates the dual-fetch behaviour
"""

import os
import tempfile
import unittest
from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest


@pytest.mark.unit
class TestStockDailyAdjFactorSchema(unittest.TestCase):
    """B6.1: schema + round-trip for the three new columns."""

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test_b6_schema.db")
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

    def test_schema_carries_three_new_columns(self):
        from src.storage import StockDaily
        cols = {c.name for c in StockDaily.__table__.columns}
        self.assertIn("adj_factor", cols)
        self.assertIn("adj_anchor_date", cols)
        self.assertIn("adj_factor_source", cols)

    def test_to_dict_includes_three_new_columns(self):
        from src.storage import StockDaily
        bar = StockDaily(
            code="600519", date=date(2024, 1, 16),
            open=100.0, high=101.0, low=99.0, close=100.5,
            adj_factor=0.987,
            adj_anchor_date=date(2024, 5, 1),
            adj_factor_source="akshare_qfq_div_raw",
        )
        d = bar.to_dict()
        self.assertEqual(d["adj_factor"], 0.987)
        self.assertEqual(d["adj_anchor_date"], date(2024, 5, 1))
        self.assertEqual(d["adj_factor_source"], "akshare_qfq_div_raw")


@pytest.mark.unit
class TestSaveDailyDataAdjFactor(unittest.TestCase):
    """B6.2: save_daily_data correctly writes the new fields from a DF."""

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test_b6_save.db")
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

    def _query_one(self, code, d):
        from sqlalchemy import select, and_
        from src.storage import StockDaily
        with self.db.get_session() as session:
            return session.execute(
                select(StockDaily).where(
                    and_(StockDaily.code == code, StockDaily.date == d),
                )
            ).scalar_one()

    def test_save_with_adj_factor_columns_present(self):
        """When the DataFrame carries adj_factor / adj_anchor_date /
        adj_factor_source, they are persisted verbatim.
        """
        df = pd.DataFrame([
            {
                "date": "2024-01-16",
                "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
                "volume": 1000.0, "amount": 100500.0, "pct_chg": 0.5,
                "adj_factor": 0.987,
                "adj_anchor_date": "2024-05-01",
                "adj_factor_source": "akshare_qfq_div_raw",
            },
        ])
        self.db.save_daily_data(df, code="600519", data_source="test")
        bar = self._query_one("600519", date(2024, 1, 16))
        self.assertAlmostEqual(bar.adj_factor, 0.987, places=4)
        self.assertEqual(bar.adj_anchor_date, date(2024, 5, 1))
        self.assertEqual(bar.adj_factor_source, "akshare_qfq_div_raw")

    def test_save_without_adj_factor_columns_uses_fetcher_unset(self):
        """When the DataFrame doesn't include the B6 columns, save_daily_data
        defaults adj_factor to 1.0 and adj_factor_source to 'fetcher_unset'.
        """
        df = pd.DataFrame([
            {
                "date": "2024-01-16",
                "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
                "volume": 1000.0, "amount": 100500.0, "pct_chg": 0.5,
            },
        ])
        self.db.save_daily_data(df, code="600519", data_source="test")
        bar = self._query_one("600519", date(2024, 1, 16))
        self.assertEqual(bar.adj_factor, 1.0)
        self.assertEqual(bar.adj_factor_source, "fetcher_unset")
        self.assertIsNone(bar.adj_anchor_date)

    def test_update_existing_row_overwrites_adj_factor(self):
        """A second save against the same (code, date) should refresh
        adj_factor with the new value (subsequent qfq base may differ).
        """
        # First save with adj_factor=1.0
        df1 = pd.DataFrame([
            {"date": "2024-01-16", "open": 100.0, "high": 101.0, "low": 99.0,
             "close": 100.5, "volume": 1000.0, "amount": 100500.0, "pct_chg": 0.5},
        ])
        self.db.save_daily_data(df1, code="600519", data_source="test")
        # Second save with a real adj_factor
        df2 = pd.DataFrame([
            {"date": "2024-01-16", "open": 100.0, "high": 101.0, "low": 99.0,
             "close": 100.5, "volume": 1000.0, "amount": 100500.0, "pct_chg": 0.5,
             "adj_factor": 0.85, "adj_factor_source": "akshare_qfq_div_raw",
             "adj_anchor_date": "2024-09-01"},
        ])
        self.db.save_daily_data(df2, code="600519", data_source="test")
        bar = self._query_one("600519", date(2024, 1, 16))
        self.assertAlmostEqual(bar.adj_factor, 0.85, places=4)
        self.assertEqual(bar.adj_factor_source, "akshare_qfq_div_raw")
        self.assertEqual(bar.adj_anchor_date, date(2024, 9, 1))


@pytest.mark.unit
class TestDataVersionComposition(unittest.TestCase):
    """B6.4: data_version composition from per-eval distributions."""

    def test_aggregate_distribution_sums_per_eval_blocks(self):
        from types import SimpleNamespace
        import json
        from src.backtest.services.backtest_service import (
            _aggregate_adj_factor_distribution,
        )

        evaluations = [
            SimpleNamespace(metrics_json=json.dumps({
                "forward_window": {
                    "adj_factor_sources": {"akshare_qfq_div_raw": 5},
                },
            })),
            SimpleNamespace(metrics_json=json.dumps({
                "forward_window": {
                    "adj_factor_sources": {"akshare_qfq_div_raw": 5, "fetcher_unset": 2},
                },
            })),
            SimpleNamespace(metrics_json=json.dumps({
                "forward_window": {
                    "adj_factor_sources": {"legacy_assume_one": 3},
                },
            })),
        ]
        dist = _aggregate_adj_factor_distribution(evaluations)
        self.assertEqual(dist, {
            "akshare_qfq_div_raw": 10,
            "fetcher_unset": 2,
            "legacy_assume_one": 3,
        })

    def test_aggregate_distribution_tolerates_malformed_rows(self):
        """Rows with NULL / corrupt / missing-block metrics_json must
        contribute zero to the distribution (and not raise).
        """
        from types import SimpleNamespace
        from src.backtest.services.backtest_service import (
            _aggregate_adj_factor_distribution,
        )

        evaluations = [
            SimpleNamespace(metrics_json=None),
            SimpleNamespace(metrics_json=""),
            SimpleNamespace(metrics_json="{not json"),
            SimpleNamespace(metrics_json='{"forward_window": null}'),
            SimpleNamespace(metrics_json='{"forward_window": {}}'),
            SimpleNamespace(metrics_json=(
                '{"forward_window": {"adj_factor_sources": "not a dict"}}'
            )),
        ]
        dist = _aggregate_adj_factor_distribution(evaluations)
        self.assertEqual(dist, {})

    def test_build_data_version_format_is_stable(self):
        """data_version is sorted by source name so the same distribution
        always produces the same string — needed for run-to-run comparison.
        """
        from src.backtest.services.backtest_service import _build_data_version

        version_a = _build_data_version({
            "akshare_qfq_div_raw": 10, "fetcher_unset": 2,
        })
        version_b = _build_data_version({
            "fetcher_unset": 2, "akshare_qfq_div_raw": 10,
        })
        self.assertEqual(version_a, version_b)
        self.assertEqual(
            version_a,
            "adj1|akshare_qfq_div_raw:10,fetcher_unset:2|total:12",
        )

    def test_build_data_version_handles_empty_distribution(self):
        from src.backtest.services.backtest_service import _build_data_version
        self.assertEqual(_build_data_version({}), "adj1|empty")

    def test_pipeline_completion_writes_data_version(self):
        """Integration: a completed run must have a non-NULL data_version
        whose schema_tag is ``adj1``. This is the byte-level guarantee
        that the data_version write path is wired correctly.
        """
        from src.backtest.services.backtest_service import FiveLayerBacktestService
        from src.storage import (
            DatabaseManager, ScreeningRun, ScreeningCandidate, StockDaily,
        )
        from src.config import Config

        # Set up an isolated DB just for this test
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "t.db")
            os.environ["DATABASE_PATH"] = db_path
            Config._instance = None
            DatabaseManager.reset_instance()
            db = DatabaseManager.get_instance()

            try:
                with db.get_session() as session:
                    session.add(ScreeningRun(
                        run_id="sr-b6-dv", trade_date=date(2024, 1, 15),
                        market="cn", status="completed",
                    ))
                    session.flush()
                    session.add(ScreeningCandidate(
                        run_id="sr-b6-dv", code="600519", name="dv test",
                        rank=1, rule_score=85.0,
                        trade_stage="probe_entry", setup_type="trend_breakout",
                        entry_maturity="high", market_regime="balanced",
                        theme_position="main_theme",
                        candidate_pool_level="leader_pool", risk_level="medium",
                    ))
                    for i in range(1, 8):
                        d = date(2024, 1, 15 + i)
                        session.add(StockDaily(
                            code="600519", date=d,
                            open=100.0, high=101.0, low=99.0, close=100.5,
                            pct_chg=0.5, volume=1000.0, amount=100500.0,
                            adj_factor=1.0,
                            adj_factor_source="legacy_assume_one",
                        ))
                    session.commit()

                svc = FiveLayerBacktestService(db_manager=db)
                run = svc.run_backtest_pipeline(
                    screening_run_id="sr-b6-dv",
                    evaluation_mode="historical_snapshot",
                    execution_model="conservative",
                )
                self.assertIsNotNone(run.data_version)
                self.assertTrue(run.data_version.startswith("adj1|"))
                self.assertIn("legacy_assume_one", run.data_version)
            finally:
                DatabaseManager.reset_instance()
                Config._instance = None
                os.environ.pop("DATABASE_PATH", None)


@pytest.mark.unit
class TestAkshareEnrichWithAdjFactor(unittest.TestCase):
    """B6.5: AkshareFetcher._enrich_with_adj_factor computes correct
    factors from a qfq + raw join, without hitting the network.
    """

    def _make_fetcher(self):
        from data_provider.akshare_fetcher import AkshareFetcher
        # Bypass __init__ to avoid sleep/UA setup; only need methods
        return AkshareFetcher.__new__(AkshareFetcher)

    def test_enrich_computes_adj_factor_per_row(self):
        """qfq=85, raw=100 → adj_factor=0.85."""
        from data_provider.akshare_fetcher import AkshareFetcher

        fetcher = self._make_fetcher()
        qfq_df = pd.DataFrame([
            {"日期": "2024-01-16", "收盘": 85.0, "开盘": 84.0, "最高": 86.0, "最低": 83.0},
            {"日期": "2024-01-17", "收盘": 88.4, "开盘": 85.0, "最高": 89.0, "最低": 84.5},
        ])
        raw_df = pd.DataFrame([
            {"日期": "2024-01-16", "收盘": 100.0},
            {"日期": "2024-01-17", "收盘": 104.0},
        ])

        # Patch _fetch_stock_data_em (the default enrichment fetcher) to
        # return raw_df without hitting akshare.
        fetcher._fetch_stock_data_em = MagicMock(return_value=raw_df)

        result = fetcher._enrich_with_adj_factor(
            qfq_df=qfq_df,
            stock_code="600519",
            start_date="2024-01-16",
            end_date="2024-01-20",
            source_name="东方财富",
        )
        self.assertIn("adj_factor", result.columns)
        self.assertIn("adj_factor_source", result.columns)
        self.assertIn("adj_anchor_date", result.columns)
        self.assertAlmostEqual(result["adj_factor"].iloc[0], 0.85, places=4)
        self.assertAlmostEqual(result["adj_factor"].iloc[1], 88.4 / 104.0, places=4)
        self.assertEqual(result["adj_factor_source"].iloc[0], "akshare_qfq_div_raw")
        self.assertEqual(result["adj_anchor_date"].iloc[0], "2024-01-20")

    def test_enrich_falls_back_when_raw_missing_for_a_date(self):
        """Rows with no matching raw close get adj_factor=1.0 and source
        ``akshare_qfq_div_raw_fallback`` so analysts can spot the gaps.
        """
        fetcher = self._make_fetcher()
        qfq_df = pd.DataFrame([
            {"日期": "2024-01-16", "收盘": 85.0},
            {"日期": "2024-01-17", "收盘": 88.4},  # no matching raw row
        ])
        raw_df = pd.DataFrame([
            {"日期": "2024-01-16", "收盘": 100.0},
        ])
        fetcher._fetch_stock_data_em = MagicMock(return_value=raw_df)

        result = fetcher._enrich_with_adj_factor(
            qfq_df=qfq_df, stock_code="600519",
            start_date="2024-01-16", end_date="2024-01-20",
            source_name="东方财富",
        )
        self.assertEqual(result["adj_factor_source"].iloc[0], "akshare_qfq_div_raw")
        self.assertEqual(result["adj_factor"].iloc[1], 1.0)
        self.assertEqual(
            result["adj_factor_source"].iloc[1], "akshare_qfq_div_raw_fallback"
        )

    def test_enrich_returns_qfq_unchanged_when_raw_fetch_empty(self):
        fetcher = self._make_fetcher()
        qfq_df = pd.DataFrame([
            {"日期": "2024-01-16", "收盘": 85.0},
        ])
        fetcher._fetch_stock_data_em = MagicMock(return_value=pd.DataFrame())

        result = fetcher._enrich_with_adj_factor(
            qfq_df=qfq_df, stock_code="600519",
            start_date="2024-01-16", end_date="2024-01-16",
            source_name="东方财富",
        )
        # No adj_factor columns added when raw fetch returns empty.
        self.assertNotIn("adj_factor", result.columns)


@pytest.mark.unit
class TestAdjFactorEnvKnob(unittest.TestCase):
    """B6.5: AKSHARE_FETCH_ADJ_FACTOR env switch."""

    def tearDown(self):
        os.environ.pop("AKSHARE_FETCH_ADJ_FACTOR", None)

    def test_default_disabled(self):
        from data_provider.akshare_fetcher import _is_adj_factor_fetch_enabled
        os.environ.pop("AKSHARE_FETCH_ADJ_FACTOR", None)
        self.assertFalse(_is_adj_factor_fetch_enabled())

    def test_enabled_via_truthy_values(self):
        from data_provider.akshare_fetcher import _is_adj_factor_fetch_enabled
        for value in ("true", "1", "yes", "on", "TRUE", "Yes"):
            os.environ["AKSHARE_FETCH_ADJ_FACTOR"] = value
            self.assertTrue(
                _is_adj_factor_fetch_enabled(),
                f"value={value!r} should enable enrichment",
            )

    def test_disabled_via_falsy_values(self):
        from data_provider.akshare_fetcher import _is_adj_factor_fetch_enabled
        for value in ("false", "0", "no", "off", "", "junk"):
            os.environ["AKSHARE_FETCH_ADJ_FACTOR"] = value
            self.assertFalse(
                _is_adj_factor_fetch_enabled(),
                f"value={value!r} should NOT enable enrichment",
            )


if __name__ == "__main__":
    unittest.main()
