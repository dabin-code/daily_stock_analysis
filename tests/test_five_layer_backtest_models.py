# -*- coding: utf-8 -*-
"""TDD RED phase: Tests for five-layer backtest ORM models.

These tests define the expected schema and behavior of the 5 new tables
before any model code is written.
"""

import json
import os
import tempfile
import unittest
from datetime import date, datetime

import pytest


@pytest.mark.unit
class TestFiveLayerBacktestModels(unittest.TestCase):
    """Verify ORM tables are created and support basic CRUD."""

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test_backtest_models.db")
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

    def test_tables_created_on_startup(self):
        """All 5 new tables should exist after DatabaseManager init."""
        from src.storage import Base
        table_names = set(Base.metadata.tables.keys())
        expected = {
            "five_layer_backtest_runs",
            "five_layer_backtest_evaluations",
            "five_layer_backtest_group_summaries",
            "five_layer_backtest_calibration_outputs",
            "five_layer_backtest_recommendations",
        }
        for name in expected:
            self.assertIn(name, table_names, f"Missing table: {name}")

    def test_create_backtest_run_minimal(self):
        """Should create a run with required fields and verify defaults."""
        from src.backtest.models.backtest_models import FiveLayerBacktestRun
        with self.db.get_session() as session:
            run = FiveLayerBacktestRun(
                backtest_run_id="run-001",
                evaluation_mode="historical_snapshot",
                execution_model="conservative",
                trade_date_from=date(2024, 1, 1),
                trade_date_to=date(2024, 1, 31),
                market="cn",
            )
            session.add(run)
            session.commit()
            self.assertEqual(run.status, "pending")
            self.assertEqual(run.sample_count, 0)
            self.assertIsNotNone(run.id)

    def test_create_evaluation_with_snapshot_fields(self):
        """Snapshot fields should be populated; replayed fields should be NULL."""
        from src.backtest.models.backtest_models import (
            FiveLayerBacktestRun, FiveLayerBacktestEvaluation,
        )
        with self.db.get_session() as session:
            run = FiveLayerBacktestRun(
                backtest_run_id="run-002",
                evaluation_mode="historical_snapshot",
                execution_model="conservative",
                trade_date_from=date(2024, 1, 1),
                trade_date_to=date(2024, 1, 31),
                market="cn",
            )
            session.add(run)
            session.flush()

            ev = FiveLayerBacktestEvaluation(
                backtest_run_id=run.backtest_run_id,
                screening_run_id="sr-001",
                screening_candidate_id=1,
                trade_date=date(2024, 1, 15),
                code="600519",
                name="贵州茅台",
                snapshot_trade_stage="probe_entry",
                snapshot_setup_type="trend_breakout",
                snapshot_entry_maturity="high",
                snapshot_market_regime="balanced",
                snapshot_theme_position="main_theme",
                snapshot_candidate_pool_level="leader_pool",
                snapshot_risk_level="medium",
                signal_family="entry",
                evaluator_type="entry",
            )
            session.add(ev)
            session.commit()

            self.assertEqual(ev.snapshot_trade_stage, "probe_entry")
            self.assertIsNone(ev.replayed_trade_stage)
            self.assertIsNone(ev.replayed_setup_type)

    def test_evaluation_unique_constraint(self):
        """Two evals with same (backtest_run_id, screening_candidate_id) should fail."""
        from sqlalchemy.exc import IntegrityError
        from src.backtest.models.backtest_models import (
            FiveLayerBacktestRun, FiveLayerBacktestEvaluation,
        )
        with self.db.get_session() as session:
            run = FiveLayerBacktestRun(
                backtest_run_id="run-003",
                evaluation_mode="historical_snapshot",
                execution_model="conservative",
                trade_date_from=date(2024, 1, 1),
                trade_date_to=date(2024, 1, 31),
                market="cn",
            )
            session.add(run)
            session.flush()

            ev1 = FiveLayerBacktestEvaluation(
                backtest_run_id="run-003",
                screening_run_id="sr-001",
                screening_candidate_id=42,
                trade_date=date(2024, 1, 15),
                code="600519",
                signal_family="entry",
                evaluator_type="entry",
            )
            ev2 = FiveLayerBacktestEvaluation(
                backtest_run_id="run-003",
                screening_run_id="sr-001",
                screening_candidate_id=42,
                trade_date=date(2024, 1, 15),
                code="600519",
                signal_family="entry",
                evaluator_type="entry",
            )
            session.add(ev1)
            session.flush()
            session.add(ev2)
            with self.assertRaises(IntegrityError):
                session.flush()

    def test_create_group_summary(self):
        """Should create a group summary and read it back."""
        from src.backtest.models.backtest_models import (
            FiveLayerBacktestRun, FiveLayerBacktestGroupSummary,
        )
        with self.db.get_session() as session:
            run = FiveLayerBacktestRun(
                backtest_run_id="run-004",
                evaluation_mode="historical_snapshot",
                execution_model="conservative",
                trade_date_from=date(2024, 1, 1),
                trade_date_to=date(2024, 1, 31),
                market="cn",
            )
            session.add(run)
            session.flush()

            summary = FiveLayerBacktestGroupSummary(
                backtest_run_id="run-004",
                group_type="signal_family",
                group_key="entry",
                sample_count=50,
                avg_return_pct=3.5,
                win_rate_pct=62.0,
            )
            session.add(summary)
            session.commit()

            self.assertEqual(summary.group_type, "signal_family")
            self.assertEqual(summary.sample_count, 50)

    def test_group_summary_to_dict_includes_aggregated_metrics(self):
        """Group summary serialization should expose all API-facing metrics."""
        from src.backtest.models.backtest_models import FiveLayerBacktestGroupSummary

        summary = FiveLayerBacktestGroupSummary(
            backtest_run_id="run-004",
            group_type="overall",
            group_key="all",
            sample_count=50,
            top_k_hit_rate=0.7,
            leader_pool_win_share=0.7,
            excess_return_pct=1.0,
            ranking_consistency=0.8,
            p25_return_pct=-0.5,
            p75_return_pct=3.0,
            extreme_sample_ratio=0.05,
            time_bucket_stability=0.1,
            profit_factor=1.8,
            avg_holding_days=4.5,
            max_consecutive_losses=3,
            plan_execution_rate=0.6,
            stage_accuracy_rate=0.7,
            system_grade="A",
        )

        payload = summary.to_dict()

        self.assertEqual(payload["top_k_hit_rate"], 0.7)
        # D2: canonical name must be exposed alongside the legacy alias.
        self.assertEqual(payload["leader_pool_win_share"], 0.7)
        self.assertEqual(payload["excess_return_pct"], 1.0)
        self.assertEqual(payload["ranking_consistency"], 0.8)
        self.assertEqual(payload["p25_return_pct"], -0.5)
        self.assertEqual(payload["p75_return_pct"], 3.0)
        self.assertEqual(payload["extreme_sample_ratio"], 0.05)
        self.assertEqual(payload["time_bucket_stability"], 0.1)
        self.assertEqual(payload["profit_factor"], 1.8)
        self.assertEqual(payload["avg_holding_days"], 4.5)
        self.assertEqual(payload["max_consecutive_losses"], 3)
        self.assertEqual(payload["plan_execution_rate"], 0.6)
        self.assertEqual(payload["stage_accuracy_rate"], 0.7)
        self.assertEqual(payload["system_grade"], "A")

    def test_create_calibration_output(self):
        """Should persist calibration output with JSON fields."""
        from src.backtest.models.backtest_models import (
            FiveLayerBacktestRun, FiveLayerBacktestCalibrationOutput,
        )
        with self.db.get_session() as session:
            run = FiveLayerBacktestRun(
                backtest_run_id="run-005",
                evaluation_mode="parameter_calibration",
                execution_model="conservative",
                trade_date_from=date(2024, 1, 1),
                trade_date_to=date(2024, 1, 31),
                market="cn",
            )
            session.add(run)
            session.flush()

            cal = FiveLayerBacktestCalibrationOutput(
                backtest_run_id="run-005",
                baseline_config_json=json.dumps({"threshold": 0.5}),
                candidate_config_json=json.dumps({"threshold": 0.7}),
                delta_metrics_json=json.dumps({"win_rate_delta": 5.2}),
                decision="accept",
                confidence=0.85,
            )
            session.add(cal)
            session.commit()

            parsed = json.loads(cal.delta_metrics_json)
            self.assertAlmostEqual(parsed["win_rate_delta"], 5.2)

    def test_create_recommendation(self):
        """Should persist a recommendation with level and evidence."""
        from src.backtest.models.backtest_models import (
            FiveLayerBacktestRun, FiveLayerBacktestRecommendation,
        )
        with self.db.get_session() as session:
            run = FiveLayerBacktestRun(
                backtest_run_id="run-006",
                evaluation_mode="historical_snapshot",
                execution_model="conservative",
                trade_date_from=date(2024, 1, 1),
                trade_date_to=date(2024, 1, 31),
                market="cn",
            )
            session.add(run)
            session.flush()

            rec = FiveLayerBacktestRecommendation(
                backtest_run_id="run-006",
                recommendation_type="threshold_adjustment",
                target_scope="setup_type",
                target_key="trend_breakout",
                current_rule="entry_maturity >= medium",
                suggested_change="entry_maturity >= high",
                recommendation_level="hypothesis",
                sample_count=30,
                confidence=0.7,
                evidence_json=json.dumps({"win_rate_low_maturity": 0.35}),
            )
            session.add(rec)
            session.commit()

            self.assertEqual(rec.recommendation_level, "hypothesis")

    def test_evaluation_to_dict(self):
        """to_dict() should include all key fields."""
        from src.backtest.models.backtest_models import (
            FiveLayerBacktestRun, FiveLayerBacktestEvaluation,
        )
        with self.db.get_session() as session:
            run = FiveLayerBacktestRun(
                backtest_run_id="run-007",
                evaluation_mode="historical_snapshot",
                execution_model="conservative",
                trade_date_from=date(2024, 1, 1),
                trade_date_to=date(2024, 1, 31),
                market="cn",
            )
            session.add(run)
            session.flush()

            ev = FiveLayerBacktestEvaluation(
                backtest_run_id="run-007",
                screening_run_id="sr-001",
                screening_candidate_id=1,
                trade_date=date(2024, 1, 15),
                code="600519",
                name="贵州茅台",
                signal_family="entry",
                evaluator_type="entry",
                signal_type="buy",
                evaluation_mode="historical_snapshot",
                snapshot_source="screening_candidate",
                replayed=False,
                snapshot_trade_stage="probe_entry",
                forward_return_1d=1.5,
                mae=-2.0,
                mfe=5.0,
                planned_entry_price=10.0,
                planned_stop_loss_price=9.2,
                planned_take_profit_price=11.0,
                actual_entry_price=10.0,
                actual_entry_date=date(2024, 1, 16),
                actual_exit_price=11.0,
                actual_exit_date=date(2024, 1, 17),
                exit_reason="take_profit",
                trade_return_pct=10.0,
                trade_replay_status="completed",
                factor_snapshot_json=json.dumps({"ma100_breakout_days": 3}),
                trade_plan_json=json.dumps({"take_profit": 5}),
            )
            session.add(ev)
            session.commit()

            d = ev.to_dict()
            self.assertIn("backtest_run_id", d)
            self.assertIn("snapshot_trade_stage", d)
            self.assertIn("forward_return_1d", d)
            self.assertIn("mae", d)
            self.assertIn("signal_type", d)
            self.assertIn("evaluation_mode", d)
            self.assertIn("snapshot_source", d)
            self.assertIn("replayed", d)
            self.assertIn("factor_snapshot_json", d)
            self.assertIn("trade_plan_json", d)
            self.assertEqual(d["planned_entry_price"], 10.0)
            self.assertEqual(d["planned_stop_loss_price"], 9.2)
            self.assertEqual(d["planned_take_profit_price"], 11.0)
            self.assertEqual(d["actual_entry_price"], 10.0)
            self.assertEqual(d["actual_entry_date"], "2024-01-16")
            self.assertEqual(d["actual_exit_price"], 11.0)
            self.assertEqual(d["actual_exit_date"], "2024-01-17")
            self.assertEqual(d["exit_reason"], "take_profit")
            self.assertEqual(d["trade_return_pct"], 10.0)
            self.assertEqual(d["trade_replay_status"], "completed")
            self.assertEqual(d["code"], "600519")

    def test_run_to_dict(self):
        """to_dict() should include all run metadata."""
        from src.backtest.models.backtest_models import FiveLayerBacktestRun
        with self.db.get_session() as session:
            run = FiveLayerBacktestRun(
                backtest_run_id="run-008",
                evaluation_mode="historical_snapshot",
                execution_model="conservative",
                trade_date_from=date(2024, 1, 1),
                trade_date_to=date(2024, 1, 31),
                market="cn",
            )
            session.add(run)
            session.commit()

            d = run.to_dict()
            self.assertIn("backtest_run_id", d)
            self.assertIn("evaluation_mode", d)
            self.assertIn("status", d)
            self.assertEqual(d["market"], "cn")


if __name__ == "__main__":
    unittest.main()
