# -*- coding: utf-8 -*-
"""TDD RED phase: Tests for five-layer backtest repositories.

These tests define the expected CRUD behavior of the repository layer
before any repository code is written.
"""

import json
import os
import tempfile
import unittest
from datetime import date, datetime

import pytest


@pytest.mark.unit
class TestRunRepository(unittest.TestCase):
    """CRUD tests for FiveLayerBacktestRun repository."""

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test_repos.db")
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

    def test_create_run(self):
        """Should create a run and return it with an id."""
        from src.backtest.repositories.run_repo import RunRepository
        repo = RunRepository(self.db)
        run = repo.create_run(
            backtest_run_id="run-r001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
            trade_date_from=date(2024, 1, 1),
            trade_date_to=date(2024, 1, 31),
            market="cn",
        )
        self.assertIsNotNone(run.id)
        self.assertEqual(run.backtest_run_id, "run-r001")
        self.assertEqual(run.status, "pending")

    def test_get_run_by_id(self):
        """Should retrieve a run by backtest_run_id."""
        from src.backtest.repositories.run_repo import RunRepository
        repo = RunRepository(self.db)
        repo.create_run(
            backtest_run_id="run-r002",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
            trade_date_from=date(2024, 1, 1),
            trade_date_to=date(2024, 1, 31),
            market="cn",
        )
        found = repo.get_run("run-r002")
        self.assertIsNotNone(found)
        self.assertEqual(found.backtest_run_id, "run-r002")

    def test_get_run_not_found(self):
        """Should return None for non-existent run."""
        from src.backtest.repositories.run_repo import RunRepository
        repo = RunRepository(self.db)
        self.assertIsNone(repo.get_run("non-existent"))

    def test_update_run_status(self):
        """Should update run status and counters."""
        from src.backtest.repositories.run_repo import RunRepository
        repo = RunRepository(self.db)
        repo.create_run(
            backtest_run_id="run-r003",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
            trade_date_from=date(2024, 1, 1),
            trade_date_to=date(2024, 1, 31),
            market="cn",
        )
        updated = repo.update_run_status(
            "run-r003",
            status="running",
            sample_count=100,
        )
        self.assertEqual(updated.status, "running")
        self.assertEqual(updated.sample_count, 100)

    def test_update_run_status_writes_every_version_field(self):
        """所有 7 个版本字段都必须有写入通道。

        原先只有 data_version 能写，其余 4 个列虽然存在却无人可写，
        运行行于是宣称自己被版本化但拿不出证据。断言用直查列的方式做，
        不经过任何合并式读回路径。
        """
        from sqlalchemy import text

        from src.backtest.repositories.run_repo import RunRepository
        repo = RunRepository(self.db)
        repo.create_run(
            backtest_run_id="run-r005",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
            trade_date_from=date(2024, 1, 1),
            trade_date_to=date(2024, 1, 31),
            market="cn",
        )
        expected = {
            "data_version": "adj1|fetcher_provided:3|total:3",
            "market_data_version": "adj1|fetcher_provided:3|total:3",
            "theme_mapping_version": "theme-2024-01",
            "candidate_snapshot_version": "snap-abc123",
            "rules_version": "rules-v7",
            "code_revision": "0" * 40,
            "config_hash": "f" * 32,
        }
        repo.update_run_status("run-r005", **expected)

        with self.db.get_session() as session:
            row = session.execute(
                text(
                    "SELECT data_version, market_data_version, theme_mapping_version, "
                    "candidate_snapshot_version, rules_version, code_revision, config_hash "
                    "FROM five_layer_backtest_runs WHERE backtest_run_id = :rid"
                ),
                {"rid": "run-r005"},
            ).fetchone()
        self.assertIsNotNone(row)
        for index, field in enumerate(expected):
            with self.subTest(field=field):
                self.assertEqual(row[index], expected[field])

    def test_list_runs_by_date_range(self):
        """Should filter runs by date range."""
        from src.backtest.repositories.run_repo import RunRepository
        repo = RunRepository(self.db)
        repo.create_run(
            backtest_run_id="run-r004a",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
            trade_date_from=date(2024, 1, 1),
            trade_date_to=date(2024, 1, 31),
            market="cn",
        )
        repo.create_run(
            backtest_run_id="run-r004b",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
            trade_date_from=date(2024, 3, 1),
            trade_date_to=date(2024, 3, 31),
            market="cn",
        )
        runs = repo.list_runs(date_from=date(2024, 1, 1), date_to=date(2024, 2, 28))
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].backtest_run_id, "run-r004a")


@pytest.mark.unit
class TestEvaluationRepository(unittest.TestCase):
    """CRUD tests for evaluation repository."""

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test_repos.db")
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

    def _create_run(self):
        from src.backtest.repositories.run_repo import RunRepository
        repo = RunRepository(self.db)
        return repo.create_run(
            backtest_run_id="run-eval",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
            trade_date_from=date(2024, 1, 1),
            trade_date_to=date(2024, 1, 31),
            market="cn",
        )

    def test_save_evaluations_batch(self):
        """Should save multiple evaluations in one batch."""
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.models.backtest_models import FiveLayerBacktestEvaluation
        self._create_run()
        repo = EvaluationRepository(self.db)

        evals = [
            FiveLayerBacktestEvaluation(
                backtest_run_id="run-eval",
                screening_candidate_id=i,
                trade_date=date(2024, 1, 15),
                code=f"60051{i}",
                signal_family="entry",
                evaluator_type="entry",
            )
            for i in range(1, 4)
        ]
        repo.save_batch(evals)

        results = repo.get_by_run("run-eval")
        self.assertEqual(len(results), 3)

    def test_get_by_run_with_signal_family_filter(self):
        """Should filter evaluations by signal_family."""
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.models.backtest_models import FiveLayerBacktestEvaluation
        self._create_run()
        repo = EvaluationRepository(self.db)

        evals = [
            FiveLayerBacktestEvaluation(
                backtest_run_id="run-eval",
                screening_candidate_id=1,
                trade_date=date(2024, 1, 15),
                code="600519",
                signal_family="entry",
                evaluator_type="entry",
            ),
            FiveLayerBacktestEvaluation(
                backtest_run_id="run-eval",
                screening_candidate_id=2,
                trade_date=date(2024, 1, 15),
                code="600520",
                signal_family="observation",
                evaluator_type="observation",
            ),
        ]
        repo.save_batch(evals)

        entries = repo.get_by_run("run-eval", signal_family="entry")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].code, "600519")

    def test_count_by_run(self):
        """Should return correct count per run."""
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.models.backtest_models import FiveLayerBacktestEvaluation
        self._create_run()
        repo = EvaluationRepository(self.db)

        evals = [
            FiveLayerBacktestEvaluation(
                backtest_run_id="run-eval",
                screening_candidate_id=i,
                trade_date=date(2024, 1, 15),
                code=f"60051{i}",
                signal_family="entry",
                evaluator_type="entry",
            )
            for i in range(1, 6)
        ]
        repo.save_batch(evals)
        self.assertEqual(repo.count_by_run("run-eval"), 5)


@pytest.mark.unit
class TestSummaryRepository(unittest.TestCase):
    """CRUD tests for summary repository."""

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test_repos.db")
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

    def _create_run(self):
        from src.backtest.repositories.run_repo import RunRepository
        repo = RunRepository(self.db)
        return repo.create_run(
            backtest_run_id="run-sum",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
            trade_date_from=date(2024, 1, 1),
            trade_date_to=date(2024, 1, 31),
            market="cn",
        )

    def test_upsert_summary_insert(self):
        """Should insert a new summary."""
        from src.backtest.repositories.summary_repo import SummaryRepository
        self._create_run()
        repo = SummaryRepository(self.db)

        summary = repo.upsert_summary(
            backtest_run_id="run-sum",
            group_type="signal_family",
            group_key="entry",
            sample_count=50,
            avg_return_pct=3.5,
            win_rate_pct=62.0,
        )
        self.assertIsNotNone(summary.id)
        self.assertEqual(summary.sample_count, 50)

    def test_upsert_summary_update(self):
        """Should update existing summary on same (run, type, key)."""
        from src.backtest.repositories.summary_repo import SummaryRepository
        self._create_run()
        repo = SummaryRepository(self.db)

        repo.upsert_summary(
            backtest_run_id="run-sum",
            group_type="signal_family",
            group_key="entry",
            sample_count=50,
            avg_return_pct=3.5,
            win_rate_pct=62.0,
        )
        updated = repo.upsert_summary(
            backtest_run_id="run-sum",
            group_type="signal_family",
            group_key="entry",
            sample_count=100,
            avg_return_pct=4.0,
            win_rate_pct=65.0,
        )
        self.assertEqual(updated.sample_count, 100)
        self.assertAlmostEqual(updated.avg_return_pct, 4.0)

        # Should still be only one row
        all_summaries = repo.get_by_run("run-sum")
        self.assertEqual(len(all_summaries), 1)

    def test_get_by_run_with_group_type_filter(self):
        """Should filter summaries by group_type."""
        from src.backtest.repositories.summary_repo import SummaryRepository
        self._create_run()
        repo = SummaryRepository(self.db)

        repo.upsert_summary(
            backtest_run_id="run-sum",
            group_type="signal_family",
            group_key="entry",
            sample_count=50,
        )
        repo.upsert_summary(
            backtest_run_id="run-sum",
            group_type="overall",
            group_key="all",
            sample_count=200,
        )

        signal_only = repo.get_by_run("run-sum", group_type="signal_family")
        self.assertEqual(len(signal_only), 1)
        self.assertEqual(signal_only[0].group_key, "entry")


if __name__ == "__main__":
    unittest.main()
