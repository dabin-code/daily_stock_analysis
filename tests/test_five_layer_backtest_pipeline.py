# -*- coding: utf-8 -*-
"""Backtest pipeline integration tests.

Full flow: create_run → select_candidates → classify → get_forward_bars →
resolve_execution → evaluate → save_evaluations → update_run_status.
"""

import json
import os
import tempfile
import unittest
from datetime import date, timedelta

import pytest


@pytest.mark.unit
class TestFiveLayerBacktestPipeline(unittest.TestCase):
    """Integration tests for the complete backtest pipeline."""

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test_bt_pipeline.db")
        os.environ["DATABASE_PATH"] = self._db_path
        from src.config import Config
        Config._instance = None
        from src.storage import DatabaseManager
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()
        self._seed_screening_data()
        self._seed_stock_daily_data()

    def tearDown(self):
        from src.storage import DatabaseManager
        from src.config import Config
        DatabaseManager.reset_instance()
        Config._instance = None
        os.environ.pop("DATABASE_PATH", None)
        self._temp_dir.cleanup()

    def _seed_screening_data(self):
        from src.storage import ScreeningRun, ScreeningCandidate
        with self.db.get_session() as session:
            run = ScreeningRun(
                run_id="sr-pipe-001",
                trade_date=date(2024, 1, 15),
                market="cn",
                status="completed",
            )
            session.add(run)
            session.flush()

            candidates = [
                ScreeningCandidate(
                    run_id="sr-pipe-001",
                    code="600519",
                    name="贵州茅台",
                    rank=1,
                    rule_score=85.0,
                    matched_strategies_json=json.dumps(["trend_breakout", "ma100_60min_combined"]),
                    rule_hits_json=json.dumps(["trend_breakout_hit", "ma100_support_hit"]),
                    factor_snapshot_json=json.dumps({
                        "close": 100.0,
                        "ma20": 98.0,
                        "pattern_123_state": "confirmed",
                    }),
                    candidate_decision_json=json.dumps({
                        "primary_strategy": "trend_breakout",
                        "contributing_strategies": ["ma100_60min_combined"],
                        "strategy_scores": {
                            "trend_breakout": 88.5,
                            "ma100_60min_combined": 81.2,
                        },
                    }),
                    trade_stage="probe_entry",
                    setup_type="trend_breakout",
                    entry_maturity="high",
                    market_regime="balanced",
                    theme_position="main_theme",
                    candidate_pool_level="leader_pool",
                    risk_level="medium",
                    trade_plan_json=json.dumps({"take_profit": 5.0, "stop_loss": -3.0}),
                ),
                ScreeningCandidate(
                    run_id="sr-pipe-001",
                    code="000858",
                    name="五粮液",
                    rank=2,
                    rule_score=72.0,
                    trade_stage="watch",
                    market_regime="balanced",
                    theme_position="related_theme",
                    candidate_pool_level="follower_pool",
                    risk_level="low",
                ),
                ScreeningCandidate(
                    run_id="sr-pipe-001",
                    code="601318",
                    name="中国平安",
                    rank=3,
                    rule_score=60.0,
                    trade_stage="stand_aside",
                    market_regime="balanced",
                    theme_position="non_theme",
                    candidate_pool_level="follower_pool",
                    risk_level="medium",
                ),
            ]
            session.add_all(candidates)
            session.commit()

    def _seed_stock_daily_data(self):
        from src.storage import StockDaily
        with self.db.get_session() as session:
            base_date = date(2024, 1, 15)
            for code, base_price in [("600519", 100.0), ("000858", 50.0), ("601318", 40.0)]:
                for i in range(1, 12):
                    d = base_date + timedelta(days=i)
                    price = base_price + i * 0.5
                    bar = StockDaily(
                        code=code,
                        date=d,
                        open=price - 0.5,
                        high=price + 2.0,
                        low=price - 2.0,
                        close=price,
                        pct_chg=0.5,
                        volume=1000000.0,
                        amount=100000000.0,
                    )
                    session.add(bar)
            session.commit()

    def _seed_p0_screening_run(self):
        from src.storage import ScreeningRun, ScreeningCandidate
        with self.db.get_session() as session:
            run = ScreeningRun(
                run_id="sr-p0-001",
                trade_date=date(2024, 2, 20),
                market="cn",
                status="completed",
            )
            session.add(run)
            session.flush()

            candidates = [
                ScreeningCandidate(
                    run_id="sr-p0-001",
                    code="300750",
                    name="宁德时代",
                    rank=1,
                    rule_score=91.0,
                    matched_strategies_json=json.dumps(
                        ["bottom_divergence_double_breakout", "volume_breakout"]
                    ),
                    rule_hits_json=json.dumps(["bottom_divergence_hit", "volume_breakout_hit"]),
                    factor_snapshot_json=json.dumps(
                        {
                            "bottom_divergence_state": "confirmed",
                            "bottom_divergence_double_breakout": True,
                            "bottom_divergence_confirmation_days": 1,
                        }
                    ),
                    candidate_decision_json=json.dumps(
                        {
                            "primary_strategy": "bottom_divergence_double_breakout",
                            "contributing_strategies": ["volume_breakout"],
                            "strategy_scores": {
                                "bottom_divergence_double_breakout": 91.0,
                                "volume_breakout": 36.0,
                            },
                        }
                    ),
                    trade_stage="probe_entry",
                    setup_type="bottom_divergence_breakout",
                    entry_maturity="high",
                    market_regime="balanced",
                    theme_position="main_theme",
                    candidate_pool_level="leader_pool",
                    risk_level="medium",
                    trade_plan_json=json.dumps({"take_profit": 7.0, "stop_loss": -4.0}),
                ),
                ScreeningCandidate(
                    run_id="sr-p0-001",
                    code="002594",
                    name="比亚迪",
                    rank=2,
                    rule_score=88.0,
                    matched_strategies_json=json.dumps(
                        ["ma100_low123_combined", "volume_breakout"]
                    ),
                    rule_hits_json=json.dumps(["ma100_low123_hit", "volume_breakout_hit"]),
                    factor_snapshot_json=json.dumps(
                        {
                            "pattern_123_state": "confirmed",
                            "ma100_low123_confirmed": True,
                            "ma100_low123_data_complete": True,
                            "ma100_low123_validation_status": "confirmed",
                        }
                    ),
                    candidate_decision_json=json.dumps(
                        {
                            "primary_strategy": "ma100_low123_combined",
                            "contributing_strategies": ["volume_breakout"],
                            "strategy_scores": {
                                "ma100_low123_combined": 88.0,
                                "volume_breakout": 34.0,
                            },
                        }
                    ),
                    trade_stage="probe_entry",
                    setup_type="low123_breakout",
                    entry_maturity="high",
                    market_regime="balanced",
                    theme_position="main_theme",
                    candidate_pool_level="leader_pool",
                    risk_level="medium",
                    trade_plan_json=json.dumps({"take_profit": 6.0, "stop_loss": -3.5}),
                ),
            ]
            session.add_all(candidates)
            session.commit()

    def _seed_p0_stock_daily_data(self):
        from src.storage import StockDaily
        with self.db.get_session() as session:
            base_date = date(2024, 2, 20)
            for code, base_price in [("300750", 180.0), ("002594", 150.0)]:
                for i in range(1, 12):
                    d = base_date + timedelta(days=i)
                    price = base_price + i * 1.2
                    session.add(
                        StockDaily(
                            code=code,
                            date=d,
                            open=price - 1.0,
                            high=price + 2.5,
                            low=price - 2.0,
                            close=price,
                            pct_chg=0.8,
                            volume=1200000.0,
                            amount=150000000.0,
                        )
                    )
            session.commit()

    def _seed_stage_recovery_screening_run(self):
        from src.storage import ScreeningRun, ScreeningCandidate
        with self.db.get_session() as session:
            run = ScreeningRun(
                run_id="sr-stage-recovery-001",
                trade_date=date(2024, 3, 18),
                market="cn",
                status="completed",
            )
            session.add(run)
            session.flush()

            candidates = [
                ScreeningCandidate(
                    run_id="sr-stage-recovery-001",
                    code="600111",
                    name="鍖楁柟绋€鍦?",
                    rank=1,
                    rule_score=89.0,
                    matched_strategies_json=json.dumps(
                        ["ma100_low123_combined", "volume_breakout"]
                    ),
                    rule_hits_json=json.dumps(["ma100_low123_hit", "volume_breakout_hit"]),
                    factor_snapshot_json=json.dumps(
                        {
                            "pattern_123_state": "confirmed",
                            "ma100_low123_confirmed": True,
                            "ma100_low123_data_complete": True,
                            "ma100_low123_validation_status": "confirmed",
                        }
                    ),
                    candidate_decision_json=json.dumps(
                        {
                            "primary_strategy": "ma100_low123_combined",
                            "contributing_strategies": ["volume_breakout"],
                            "strategy_scores": {
                                "ma100_low123_combined": 89.0,
                                "volume_breakout": 35.0,
                            },
                        }
                    ),
                    trade_stage="focus",
                    setup_type="low123_breakout",
                    entry_maturity="high",
                    market_regime="balanced",
                    theme_position="main_theme",
                    candidate_pool_level="leader_pool",
                    risk_level="medium",
                    trade_plan_json=json.dumps({"take_profit": 6.5, "stop_loss": -3.2}),
                ),
                ScreeningCandidate(
                    run_id="sr-stage-recovery-001",
                    code="600222",
                    name="姹熻嫃闃冲厜",
                    rank=2,
                    rule_score=84.0,
                    matched_strategies_json=json.dumps(
                        ["ma100_low123_combined", "volume_breakout"]
                    ),
                    rule_hits_json=json.dumps(["ma100_low123_hit"]),
                    factor_snapshot_json=json.dumps(
                        {
                            "pattern_123_state": "confirmed",
                            "ma100_low123_confirmed": True,
                            "ma100_low123_data_complete": False,
                            "ma100_low123_validation_status": "confirmed_missing_breakout_bar_index",
                            "ma100_low123_validation_reason": "confirmed_missing_breakout_bar_index",
                        }
                    ),
                    candidate_decision_json=json.dumps(
                        {
                            "primary_strategy": "ma100_low123_combined",
                            "contributing_strategies": [],
                            "strategy_scores": {
                                "ma100_low123_combined": 84.0,
                            },
                        }
                    ),
                    trade_stage="focus",
                    setup_type="low123_breakout",
                    entry_maturity="high",
                    market_regime="balanced",
                    theme_position="main_theme",
                    candidate_pool_level="leader_pool",
                    risk_level="medium",
                    trade_plan_json=json.dumps({"take_profit": 6.0, "stop_loss": -3.0}),
                ),
            ]
            session.add_all(candidates)
            session.commit()

    def _seed_stage_recovery_stock_daily_data(self):
        from src.storage import StockDaily
        with self.db.get_session() as session:
            base_date = date(2024, 3, 18)
            for code, base_price in [("600111", 80.0), ("600222", 42.0)]:
                for i in range(1, 12):
                    d = base_date + timedelta(days=i)
                    price = base_price + i * 0.8
                    session.add(
                        StockDaily(
                            code=code,
                            date=d,
                            open=price - 0.4,
                            high=price + 1.5,
                            low=price - 1.0,
                            close=price,
                            pct_chg=0.6,
                            volume=900000.0,
                            amount=90000000.0,
                        )
                    )
            session.commit()

    def test_pipeline_creates_run(self):
        """Pipeline should create a run with correct metadata."""
        from src.backtest.services.backtest_service import FiveLayerBacktestService
        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="sr-pipe-001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )
        self.assertIsNotNone(run)
        self.assertEqual(run.evaluation_mode, "historical_snapshot")
        self.assertEqual(run.execution_model, "conservative")

    def test_pipeline_run_status_completed(self):
        from src.backtest.services.backtest_service import FiveLayerBacktestService
        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="sr-pipe-001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )
        self.assertEqual(run.status, "completed")

    def test_pipeline_creates_evaluations(self):
        """Should create one evaluation per candidate."""
        from src.backtest.services.backtest_service import FiveLayerBacktestService
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="sr-pipe-001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )
        eval_repo = EvaluationRepository(self.db)
        evals = eval_repo.get_by_run(run.backtest_run_id)
        self.assertEqual(len(evals), 3)

    def test_pipeline_entry_signal_has_metrics(self):
        """Entry signal (probe_entry) should have forward_return_1d and MAE/MFE."""
        from src.backtest.services.backtest_service import FiveLayerBacktestService
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="sr-pipe-001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )
        eval_repo = EvaluationRepository(self.db)
        entries = eval_repo.get_by_run(run.backtest_run_id, signal_family="entry")
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.code, "600519")
        self.assertIsNotNone(entry.forward_return_1d)
        self.assertIsNotNone(entry.mae)
        self.assertIsNotNone(entry.mfe)

    def test_pipeline_observation_signal_has_metrics(self):
        """Observation signal should have risk_avoided_pct and opportunity_cost_pct."""
        from src.backtest.services.backtest_service import FiveLayerBacktestService
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="sr-pipe-001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )
        eval_repo = EvaluationRepository(self.db)
        observations = eval_repo.get_by_run(run.backtest_run_id, signal_family="observation")
        self.assertEqual(len(observations), 2)
        for obs in observations:
            self.assertIsNotNone(obs.risk_avoided_pct)
            self.assertIsNotNone(obs.opportunity_cost_pct)

    def test_pipeline_snapshot_fields_populated(self):
        """Snapshot fields should be populated from screening candidates."""
        from src.backtest.services.backtest_service import FiveLayerBacktestService
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="sr-pipe-001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )
        eval_repo = EvaluationRepository(self.db)
        entries = eval_repo.get_by_run(run.backtest_run_id, signal_family="entry")
        entry = entries[0]
        self.assertEqual(entry.snapshot_trade_stage, "probe_entry")
        self.assertEqual(entry.snapshot_setup_type, "trend_breakout")
        self.assertEqual(entry.snapshot_market_regime, "balanced")

    def test_pipeline_persists_factor_and_trade_plan_json(self):
        """Entry evaluation should retain factor snapshot and trade plan JSON for later tracing."""
        from src.backtest.services.backtest_service import FiveLayerBacktestService
        from src.backtest.repositories.evaluation_repo import EvaluationRepository

        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="sr-pipe-001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )
        eval_repo = EvaluationRepository(self.db)
        entry = eval_repo.get_by_run(run.backtest_run_id, signal_family="entry")[0]

        self.assertIsNotNone(entry.factor_snapshot_json)
        self.assertIn("pattern_123_state", entry.factor_snapshot_json)
        self.assertIsNotNone(entry.trade_plan_json)
        self.assertIn("take_profit", entry.trade_plan_json)

    def test_pipeline_persists_evidence_json_with_strategy_attribution(self):
        """Entry evaluation should retain matched strategies and attribution for strategy-cohort analysis."""
        from src.backtest.services.backtest_service import FiveLayerBacktestService
        from src.backtest.repositories.evaluation_repo import EvaluationRepository

        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="sr-pipe-001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )
        eval_repo = EvaluationRepository(self.db)
        entry = eval_repo.get_by_run(run.backtest_run_id, signal_family="entry")[0]

        self.assertIsNotNone(entry.evidence_json)
        evidence = json.loads(entry.evidence_json)
        self.assertCountEqual(
            evidence["matched_strategies"],
            ["trend_breakout", "ma100_60min_combined"],
        )
        self.assertEqual(evidence["primary_strategy"], "trend_breakout")
        self.assertCountEqual(
            evidence["contributing_strategies"],
            ["ma100_60min_combined"],
        )
        self.assertAlmostEqual(evidence["strategy_scores"]["trend_breakout"], 88.5)

    def test_pipeline_preserves_bottom_divergence_p0_strategy_in_evidence(self):
        """P0 sample: bottom divergence primary strategy should survive into backtest evidence."""
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.services.backtest_service import FiveLayerBacktestService

        self._seed_p0_screening_run()
        self._seed_p0_stock_daily_data()

        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="sr-p0-001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )

        eval_repo = EvaluationRepository(self.db)
        entries = eval_repo.get_by_run(run.backtest_run_id, signal_family="entry")
        self.assertEqual({entry.code for entry in entries}, {"300750", "002594"})
        entries_by_code = {entry.code: entry for entry in entries}
        bottom_divergence = entries_by_code["300750"]

        evidence = json.loads(bottom_divergence.evidence_json)
        self.assertEqual(
            evidence["primary_strategy"],
            "bottom_divergence_double_breakout",
        )
        self.assertCountEqual(evidence["contributing_strategies"], ["volume_breakout"])
        self.assertEqual(bottom_divergence.snapshot_setup_type, "bottom_divergence_breakout")
        self.assertIn(
            "bottom_divergence_double_breakout",
            evidence["matched_strategies"],
        )

    def test_pipeline_preserves_low123_setup_and_ma100_strategy_in_evidence(self):
        """P0 sample: low123 setup should keep ma100_low123 strategy attribution."""
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.services.backtest_service import FiveLayerBacktestService

        self._seed_p0_screening_run()
        self._seed_p0_stock_daily_data()

        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="sr-p0-001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )

        eval_repo = EvaluationRepository(self.db)
        entries = eval_repo.get_by_run(run.backtest_run_id, signal_family="entry")
        self.assertEqual({entry.code for entry in entries}, {"300750", "002594"})
        entries_by_code = {entry.code: entry for entry in entries}
        low123 = entries_by_code["002594"]

        evidence = json.loads(low123.evidence_json)
        factor_snapshot = json.loads(low123.factor_snapshot_json)
        self.assertEqual(evidence["primary_strategy"], "ma100_low123_combined")
        self.assertCountEqual(evidence["contributing_strategies"], ["volume_breakout"])
        self.assertEqual(low123.snapshot_setup_type, "low123_breakout")
        self.assertTrue(factor_snapshot["ma100_low123_confirmed"])
        self.assertEqual(factor_snapshot["ma100_low123_validation_status"], "confirmed")

    def test_pipeline_recovers_entry_under_rule_replay_when_snapshot_was_too_conservative(self):
        """Under ``rule_replay``, strong entry snapshots should be replayed
        into entries even when persisted trade_stage was conservative.

        Regression guard for A2/A3: previously this re-classification ran
        unconditionally, silently rewriting historical_snapshot results.
        """
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.services.backtest_service import FiveLayerBacktestService

        self._seed_stage_recovery_screening_run()
        self._seed_stage_recovery_stock_daily_data()

        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="sr-stage-recovery-001",
            evaluation_mode="rule_replay",
            execution_model="conservative",
        )

        eval_repo = EvaluationRepository(self.db)
        entries = eval_repo.get_by_run(run.backtest_run_id, signal_family="entry")
        self.assertEqual({entry.code for entry in entries}, {"600111"})

        recovered = entries[0]
        # Snapshot is preserved verbatim; replayed_* documents the diff
        self.assertEqual(recovered.snapshot_trade_stage, "focus")
        self.assertEqual(recovered.replayed_trade_stage, "probe_entry")
        self.assertTrue(recovered.replayed)
        self.assertEqual(recovered.snapshot_source, "replayed")
        self.assertEqual(recovered.signal_type, "probe_entry")
        self.assertIsNotNone(recovered.forward_return_5d)
        self.assertIsNotNone(recovered.plan_success)

        metrics = json.loads(recovered.metrics_json)
        self.assertEqual(metrics["effective_trade_stage"], "probe_entry")
        self.assertEqual(metrics["entry_timing_label"], "on_time")

        config = json.loads(run.config_json)
        self.assertEqual(config["sample_baseline"]["entry_sample_count"], 1)
        self.assertEqual(config["sample_baseline"]["observation_sample_count"], 1)

    def test_pipeline_historical_snapshot_does_not_replay_conservative_trade_stage(self):
        """Under ``historical_snapshot``, a candidate persisted as ``focus``
        must remain an observation — the pipeline must NOT silently replay
        rules that elevate it to entry, otherwise we would manufacture
        history that never existed at decision time.

        Regression guard for A2: previously the same recovery logic ran
        regardless of evaluation_mode, inflating entry sample counts in
        snapshot mode.
        """
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.services.backtest_service import FiveLayerBacktestService

        self._seed_stage_recovery_screening_run()
        self._seed_stage_recovery_stock_daily_data()

        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="sr-stage-recovery-001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )

        eval_repo = EvaluationRepository(self.db)
        evals = eval_repo.get_by_run(run.backtest_run_id)
        entries = [e for e in evals if e.signal_family == "entry"]
        observations = [e for e in evals if e.signal_family == "observation"]

        # Both candidates were persisted with trade_stage="focus" — both
        # must stay as observations under historical_snapshot.
        self.assertEqual(entries, [])
        self.assertEqual({e.code for e in observations}, {"600111", "600222"})

        for ev in evals:
            self.assertEqual(ev.snapshot_source, "screening_candidate")
            self.assertFalse(ev.replayed)
            self.assertIsNone(ev.replayed_trade_stage)
            # snapshot_trade_stage stays verbatim
            self.assertEqual(ev.snapshot_trade_stage, "focus")

    def test_pipeline_keeps_missing_breakout_index_case_in_observation_under_replay(self):
        """Even under ``rule_replay``, the conservative low123 guard for
        ``confirmed_missing_breakout_bar_index`` must still hold: that
        candidate stays observation because the evidence is incomplete.
        """
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.services.backtest_service import FiveLayerBacktestService

        self._seed_stage_recovery_screening_run()
        self._seed_stage_recovery_stock_daily_data()

        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="sr-stage-recovery-001",
            evaluation_mode="rule_replay",
            execution_model="conservative",
        )

        eval_repo = EvaluationRepository(self.db)
        observations = eval_repo.get_by_run(run.backtest_run_id, signal_family="observation")
        guarded = next(item for item in observations if item.code == "600222")

        self.assertEqual(guarded.snapshot_trade_stage, "focus")
        self.assertEqual(guarded.signal_type, "focus")
        # Replay kept the conservative decision: replayed_trade_stage must
        # mirror the snapshot, NOT be promoted to probe_entry.
        self.assertEqual(guarded.replayed_trade_stage, "focus")
        self.assertFalse(guarded.replayed)
        self.assertIsNotNone(guarded.risk_avoided_pct)

        factor_snapshot = json.loads(guarded.factor_snapshot_json)
        self.assertEqual(
            factor_snapshot["ma100_low123_validation_status"],
            "confirmed_missing_breakout_bar_index",
        )

    def test_pipeline_persists_sample_bucket_and_timing_metrics_for_entry(self):
        """Entry evaluation should persist selected/core bucket and timing labels in metrics_json."""
        from src.backtest.services.backtest_service import FiveLayerBacktestService
        from src.backtest.repositories.evaluation_repo import EvaluationRepository

        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="sr-pipe-001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )
        eval_repo = EvaluationRepository(self.db)
        entry = eval_repo.get_by_run(run.backtest_run_id, signal_family="entry")[0]

        self.assertIsNotNone(entry.metrics_json)
        metrics = json.loads(entry.metrics_json)
        self.assertEqual(metrics["sample_origin"], "selected")
        self.assertEqual(metrics["sample_bucket"], "core")
        self.assertEqual(metrics["entry_timing_label"], "on_time")
        self.assertIn("early_pullback_pct", metrics)
        self.assertIn("late_entry_gap_pct", metrics)

    def test_pipeline_persists_sample_bucket_for_observation(self):
        """Observation evaluation should persist selected origin and non-entry timing label."""
        from src.backtest.services.backtest_service import FiveLayerBacktestService
        from src.backtest.repositories.evaluation_repo import EvaluationRepository

        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="sr-pipe-001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )
        eval_repo = EvaluationRepository(self.db)
        observation = eval_repo.get_by_run(run.backtest_run_id, signal_family="observation")[0]

        self.assertIsNotNone(observation.metrics_json)
        metrics = json.loads(observation.metrics_json)
        self.assertEqual(metrics["sample_origin"], "selected")
        self.assertEqual(metrics["sample_bucket"], "boundary")
        self.assertEqual(metrics["entry_timing_label"], "not_applicable")

    def test_pipeline_replayed_fields_null_in_snapshot_mode(self):
        """In historical_snapshot mode, replayed_* fields must be NULL."""
        from src.backtest.services.backtest_service import FiveLayerBacktestService
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="sr-pipe-001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )
        eval_repo = EvaluationRepository(self.db)
        evals = eval_repo.get_by_run(run.backtest_run_id)
        for ev in evals:
            self.assertIsNone(ev.replayed_trade_stage)
            self.assertIsNone(ev.replayed_setup_type)
            self.assertIsNone(ev.replayed_market_regime)

    def test_pipeline_run_counters_updated(self):
        from src.backtest.services.backtest_service import FiveLayerBacktestService
        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="sr-pipe-001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )
        self.assertEqual(run.sample_count, 3)
        self.assertEqual(run.completed_count, 3)
        self.assertEqual(run.error_count, 0)

    def test_pipeline_persists_run_sample_baseline_in_config_json(self):
        """Completed runs should persist a sample baseline so API consumers can explain raw vs aggregatable counts."""
        from src.backtest.services.backtest_service import FiveLayerBacktestService

        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="sr-pipe-001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )

        self.assertIsNotNone(run.config_json)
        config = json.loads(run.config_json)
        self.assertEqual(
            config["sample_baseline"],
            {
                "raw_sample_count": 3,
                "evaluated_sample_count": 3,
                "aggregatable_sample_count": 3,
                "entry_sample_count": 1,
                "observation_sample_count": 2,
                "error_sample_count": 0,
                "suppressed_sample_count": 0,
                "suppressed_reasons": {},
            },
        )

    def test_pipeline_entry_fill_status(self):
        """Entry evaluation should have fill status from execution model."""
        from src.backtest.services.backtest_service import FiveLayerBacktestService
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="sr-pipe-001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )
        eval_repo = EvaluationRepository(self.db)
        entries = eval_repo.get_by_run(run.backtest_run_id, signal_family="entry")
        entry = entries[0]
        self.assertEqual(entry.entry_fill_status, "filled")
        self.assertIsNotNone(entry.entry_fill_price)

    def test_pipeline_empty_screening_run(self):
        """Pipeline with non-existent screening_run_id should complete with 0 samples."""
        from src.backtest.services.backtest_service import FiveLayerBacktestService
        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="non-existent",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.sample_count, 0)

    def test_pipeline_eval_status_evaluated(self):
        """Each evaluation should have eval_status='evaluated'."""
        from src.backtest.services.backtest_service import FiveLayerBacktestService
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="sr-pipe-001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )
        eval_repo = EvaluationRepository(self.db)
        evals = eval_repo.get_by_run(run.backtest_run_id)
        for ev in evals:
            self.assertEqual(ev.eval_status, "evaluated")
            self.assertIsNone(ev.suppression_reason)

    def test_pipeline_persists_error_row_when_candidate_evaluation_raises(self):
        """When _process_candidate raises, the pipeline must persist a placeholder
        evaluation row with eval_status='error' and a suppression_reason that
        identifies the exception class.

        Regression guard for E1: errors used to be silently dropped (only the
        run.error_count counter went up), making it impossible to attribute a
        missing sample to its real cause.
        """
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.services.backtest_service import FiveLayerBacktestService

        svc = FiveLayerBacktestService(db_manager=self.db)

        original = svc._process_candidate

        def _explode_for_first_candidate(*, run, candidate, **kwargs):
            if candidate.get("code") == "600519":
                raise RuntimeError("forced failure for test")
            return original(run=run, candidate=candidate, **kwargs)

        svc._process_candidate = _explode_for_first_candidate

        run = svc.run_backtest_pipeline(
            screening_run_id="sr-pipe-001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )
        self.assertEqual(run.error_count, 1)

        eval_repo = EvaluationRepository(self.db)
        evals = eval_repo.get_by_run(run.backtest_run_id)
        error_rows = [e for e in evals if e.eval_status == "error"]
        self.assertEqual(len(error_rows), 1)

        error_row = error_rows[0]
        self.assertEqual(error_row.code, "600519")
        self.assertEqual(error_row.suppression_reason, "exception:RuntimeError")
        self.assertEqual(error_row.signal_family, "unknown")
        # Placeholder rows must not contribute to forward metrics
        self.assertIsNone(error_row.forward_return_5d)
        self.assertIsNone(error_row.risk_avoided_pct)

        metrics = json.loads(error_row.metrics_json)
        self.assertEqual(metrics["error_class"], "RuntimeError")
        self.assertIn("forced failure", metrics["error_message"])

    def test_pipeline_marks_entry_suppressed_when_no_forward_bars(self):
        """Entry candidates whose stock has no forward bars must be persisted as
        suppressed, not silently labelled as evaluated with empty metrics.

        Regression guard for E2: previously the row stayed eval_status='evaluated'
        with everything NULL, so it was indistinguishable from a real loss.
        """
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.services.backtest_service import FiveLayerBacktestService
        from src.storage import ScreeningCandidate, ScreeningRun

        # Seed a candidate referencing a stock with no daily bars
        with self.db.get_session() as session:
            session.add(
                ScreeningRun(
                    run_id="sr-no-bars-001",
                    trade_date=date(2024, 5, 6),
                    market="cn",
                    status="completed",
                )
            )
            session.flush()
            session.add(
                ScreeningCandidate(
                    run_id="sr-no-bars-001",
                    code="999999",
                    name="无数据股票",
                    rank=1,
                    rule_score=80.0,
                    trade_stage="probe_entry",
                    setup_type="trend_breakout",
                    entry_maturity="high",
                    market_regime="balanced",
                    theme_position="main_theme",
                    candidate_pool_level="leader_pool",
                    risk_level="medium",
                    trade_plan_json=json.dumps({"take_profit": 5.0, "stop_loss": -3.0}),
                )
            )
            session.commit()

        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="sr-no-bars-001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )

        eval_repo = EvaluationRepository(self.db)
        evals = eval_repo.get_by_run(run.backtest_run_id)
        self.assertEqual(len(evals), 1)
        self.assertEqual(evals[0].eval_status, "suppressed")
        self.assertEqual(evals[0].suppression_reason, "no_forward_bars")
        self.assertIsNone(evals[0].forward_return_5d)

        config = json.loads(run.config_json)
        # Suppression reason should propagate into the run-level baseline
        self.assertEqual(
            config["sample_baseline"]["suppressed_reasons"],
            {"no_forward_bars": 1},
        )
        self.assertEqual(config["sample_baseline"]["aggregatable_sample_count"], 0)
        self.assertEqual(config["sample_baseline"]["error_sample_count"], 0)

    def test_pipeline_persists_candidate_filter_for_by_screening_run_source(self):
        """run_backtest_pipeline must record source='by_screening_run' and the
        requested screening_run_id in candidate_filter_json so any backtest
        run is auditable back to its candidate origin (A1).
        """
        from src.backtest.services.backtest_service import FiveLayerBacktestService

        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="sr-pipe-001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
            eval_window_days=10,
        )

        self.assertIsNotNone(run.candidate_filter_json)
        candidate_filter = json.loads(run.candidate_filter_json)
        self.assertEqual(candidate_filter["source"], "by_screening_run")
        self.assertEqual(candidate_filter["requested_screening_run_id"], "sr-pipe-001")
        self.assertEqual(candidate_filter["screening_run_ids"], ["sr-pipe-001"])
        self.assertEqual(candidate_filter["screening_run_count"], 1)
        self.assertEqual(candidate_filter["evaluation_mode"], "historical_snapshot")
        self.assertEqual(candidate_filter["execution_model"], "conservative")
        self.assertEqual(candidate_filter["eval_window_days"], 10)
        self.assertEqual(candidate_filter["market"], "cn")

    def test_pipeline_persists_candidate_filter_for_empty_screening_run(self):
        """Even when 0 candidates match, the requested screening_run_id must
        still appear in candidate_filter_json so empty backtest runs remain
        attributable to their source.
        """
        from src.backtest.services.backtest_service import FiveLayerBacktestService

        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="non-existent-run",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )

        self.assertEqual(run.sample_count, 0)
        self.assertIsNotNone(run.candidate_filter_json)
        candidate_filter = json.loads(run.candidate_filter_json)
        self.assertEqual(candidate_filter["source"], "by_screening_run")
        self.assertEqual(candidate_filter["requested_screening_run_id"], "non-existent-run")
        self.assertIn("non-existent-run", candidate_filter["screening_run_ids"])

    def test_run_backtest_persists_candidate_filter_aggregating_screening_runs(self):
        """run_backtest by date-range must aggregate every screening_run_id it
        sourced candidates from, so the run is auditable without re-querying
        screening tables (A1).
        """
        from src.backtest.services.backtest_service import FiveLayerBacktestService
        from src.storage import ScreeningCandidate, ScreeningRun

        # Seed a second screening run on the same trade_date so a date-range
        # backtest must aggregate two screening_run_ids.
        with self.db.get_session() as session:
            session.add(
                ScreeningRun(
                    run_id="sr-pipe-002",
                    trade_date=date(2024, 1, 15),
                    market="cn",
                    status="completed",
                )
            )
            session.flush()
            session.add(
                ScreeningCandidate(
                    run_id="sr-pipe-002",
                    code="600519",
                    name="贵州茅台",
                    rank=1,
                    rule_score=80.0,
                    trade_stage="probe_entry",
                    setup_type="trend_pullback",
                    entry_maturity="medium",
                    market_regime="balanced",
                    theme_position="related_theme",
                    candidate_pool_level="follower_pool",
                    risk_level="medium",
                    trade_plan_json=json.dumps({"take_profit": 4.0, "stop_loss": -2.5}),
                )
            )
            session.commit()

        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest(
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
            trade_date_from=date(2024, 1, 15),
            trade_date_to=date(2024, 1, 15),
            market="cn",
            eval_window_days=10,
        )

        self.assertIsNotNone(run.candidate_filter_json)
        candidate_filter = json.loads(run.candidate_filter_json)
        self.assertEqual(candidate_filter["source"], "by_date_range")
        self.assertEqual(candidate_filter["screening_run_count"], 2)
        self.assertEqual(
            candidate_filter["screening_run_ids"],
            ["sr-pipe-001", "sr-pipe-002"],
        )
        self.assertEqual(candidate_filter["trade_date_from"], "2024-01-15")
        self.assertEqual(candidate_filter["trade_date_to"], "2024-01-15")
        self.assertEqual(candidate_filter["market"], "cn")

    def test_pipeline_records_no_candidate_staleness_when_clean_run(self):
        """Happy-path runs should still emit candidate_staleness with stale_count=0
        so downstream consumers can rely on the field being present (A4 lite).
        """
        from src.backtest.services.backtest_service import FiveLayerBacktestService

        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="sr-pipe-001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )

        config = json.loads(run.config_json)
        self.assertIn("candidate_staleness", config)
        staleness = config["candidate_staleness"]
        self.assertEqual(staleness["stale_count"], 0)
        self.assertEqual(staleness["stale_samples"], [])
        self.assertIn("anchor", staleness)

    def test_compute_summaries_persists_grade_breakdown_in_overall_metrics_json(self):
        """F3: compute_summaries must enrich overall.metrics_json with a
        ``system_grade_breakdown`` block (grade + raw_grade + reasons +
        aggregatable_ratio + downgraded flag), without clobbering the
        sample_baseline / family_breakdown blocks the aggregator already
        wrote there.
        """
        from src.backtest.repositories.summary_repo import SummaryRepository
        from src.backtest.services.backtest_service import FiveLayerBacktestService

        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="sr-pipe-001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )
        svc.compute_summaries(run.backtest_run_id)

        overall = next(
            s for s in SummaryRepository(self.db).get_by_run(run.backtest_run_id)
            if s.group_type == "overall"
        )
        self.assertIsNotNone(overall.metrics_json)
        metrics = json.loads(overall.metrics_json)
        # Aggregator-written blocks must still be there
        self.assertIn("sample_baseline", metrics)
        # Grade breakdown must be added on top
        self.assertIn("system_grade_breakdown", metrics)
        breakdown = metrics["system_grade_breakdown"]
        self.assertEqual(breakdown["grade"], overall.system_grade)
        self.assertIn("raw_grade", breakdown)
        self.assertIn("reasons", breakdown)
        self.assertIsInstance(breakdown["reasons"], list)
        self.assertIn("downgraded", breakdown)
        self.assertIn("aggregatable_ratio", breakdown)

    def test_compute_summaries_grades_with_family_correct_metrics(self):
        """Patch 1 (key fix): SystemGrader must be fed family_breakdown.entry
        win_rate / profit_factor when present, NOT the mixed
        overall.win_rate_pct / overall.profit_factor that average entry's
        forward_return_5d with observation's risk_avoided_pct (the latter is
        non-negative by construction and silently inflates profit_factor).

        Verify:
          * system_grade_breakdown.metric_source == "entry" when entry data
            is present in family_breakdown.
          * The metric_inputs persisted for audit match the entry sub-block,
            not the overall (mixed) summary fields.
        """
        from src.backtest.repositories.summary_repo import SummaryRepository
        from src.backtest.services.backtest_service import FiveLayerBacktestService

        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id="sr-pipe-001",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )
        svc.compute_summaries(run.backtest_run_id)

        overall = next(
            s for s in SummaryRepository(self.db).get_by_run(run.backtest_run_id)
            if s.group_type == "overall"
        )
        metrics = json.loads(overall.metrics_json)
        breakdown = metrics["system_grade_breakdown"]
        family_breakdown = metrics.get("family_breakdown") or {}
        entry_metrics = family_breakdown.get("entry") or {}

        # Pipeline seeds at least one entry-eligible candidate (probe_entry),
        # so family_breakdown.entry must be populated and chosen.
        self.assertTrue(
            entry_metrics.get("win_rate_pct") is not None,
            "Pipeline should produce at least one aggregatable entry sample",
        )
        self.assertEqual(
            breakdown["metric_source"], "entry",
            "SystemGrader must be fed family_breakdown.entry when present",
        )
        # The audited metric_inputs must echo the entry sub-block exactly,
        # not the mixed overall fields, so reviewers can verify which numbers
        # actually drove the headline grade.
        self.assertEqual(
            breakdown["metric_inputs"]["win_rate_pct"],
            entry_metrics.get("win_rate_pct"),
        )
        self.assertEqual(
            breakdown["metric_inputs"]["profit_factor"],
            entry_metrics.get("profit_factor"),
        )
        self.assertEqual(
            breakdown["metric_inputs"]["time_bucket_stability"],
            entry_metrics.get("time_bucket_stability"),
        )

    def test_detect_candidate_staleness_flags_rewrites_after_anchor(self):
        """Unit-level guard for A4 lite: candidates whose updated_at is strictly
        later than the anchor must be returned, while those rewritten before
        (or exactly at) the anchor must be skipped. The returned shape must
        also be JSON-serialisable (datetimes converted to isoformat).
        """
        from datetime import datetime
        from src.backtest.services.backtest_service import _detect_candidate_staleness

        anchor = datetime(2024, 1, 15, 9, 0, 0)
        candidates = [
            # rewritten AFTER anchor → flagged
            {
                "screening_run_id": "sr-A",
                "code": "600519",
                "candidate_created_at": datetime(2024, 1, 14, 8, 0, 0),
                "candidate_updated_at": datetime(2024, 1, 15, 9, 30, 0),
            },
            # written BEFORE anchor → clean
            {
                "screening_run_id": "sr-A",
                "code": "000858",
                "candidate_created_at": datetime(2024, 1, 14, 8, 0, 0),
                "candidate_updated_at": datetime(2024, 1, 14, 8, 0, 0),
            },
            # exactly at anchor → clean (boundary is non-stale)
            {
                "screening_run_id": "sr-A",
                "code": "601318",
                "candidate_created_at": datetime(2024, 1, 14, 8, 0, 0),
                "candidate_updated_at": anchor,
            },
            # missing updated_at → skipped (cannot determine staleness)
            {
                "screening_run_id": "sr-A",
                "code": "999999",
                "candidate_created_at": None,
                "candidate_updated_at": None,
            },
        ]

        stale = _detect_candidate_staleness(candidates, anchor)

        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["code"], "600519")
        self.assertEqual(stale[0]["screening_run_id"], "sr-A")
        # Timestamps must be ISO-formatted strings (JSON-safe)
        self.assertEqual(stale[0]["candidate_updated_at"], "2024-01-15T09:30:00")
        self.assertEqual(stale[0]["candidate_created_at"], "2024-01-14T08:00:00")

    def test_save_screening_candidates_preserves_first_seen_created_at(self):
        """Re-saving the same (run_id, code) must NOT reset created_at — it is
        the decision-time anchor used by candidate_staleness detection.
        """
        from src.storage import ScreeningCandidate

        with self.db.get_session() as session:
            original = (
                session.query(ScreeningCandidate)
                .filter(
                    ScreeningCandidate.run_id == "sr-pipe-001",
                    ScreeningCandidate.code == "000858",
                )
                .one()
            )
            original_created_at = original.created_at

        self.db.save_screening_candidates(
            run_id="sr-pipe-001",
            candidates=[
                {
                    "code": "000858",
                    "name": "五粮液",
                    "rank": 2,
                    "rule_score": 95.0,  # changed
                    "trade_stage": "watch",
                    "market_regime": "balanced",
                    "theme_position": "related_theme",
                    "candidate_pool_level": "follower_pool",
                    "risk_level": "low",
                },
            ],
        )

        with self.db.get_session() as session:
            rewritten = (
                session.query(ScreeningCandidate)
                .filter(
                    ScreeningCandidate.run_id == "sr-pipe-001",
                    ScreeningCandidate.code == "000858",
                )
                .one()
            )
            self.assertEqual(rewritten.created_at, original_created_at)
            self.assertIsNotNone(rewritten.updated_at)
            self.assertGreaterEqual(rewritten.updated_at, original_created_at)
            self.assertEqual(rewritten.rule_score, 95.0)  # rewrite landed

    def test_run_baseline_prefers_suppression_reason_over_legacy_inference(self):
        """When suppression_reason is present, baseline must use it instead of
        the legacy ``signal_family``-based inference.

        Regression guard for E2 (read path): aggregator and run baseline
        previously inferred a generic family code at read time, hiding the
        real cause (e.g. limit_blocked, no_forward_bars, exception).
        """
        from src.backtest.models.backtest_models import FiveLayerBacktestEvaluation
        from src.backtest.services.backtest_service import _build_run_sample_baseline

        evaluations = [
            FiveLayerBacktestEvaluation(
                signal_family="entry",
                eval_status="suppressed",
                suppression_reason="exec_not_filled:limit_up_blocked",
            ),
            FiveLayerBacktestEvaluation(
                signal_family="entry",
                eval_status="suppressed",
                suppression_reason="no_forward_bars",
            ),
            FiveLayerBacktestEvaluation(
                signal_family="entry",
                eval_status="error",
                suppression_reason="exception:RuntimeError",
            ),
            # Legacy row (no suppression_reason) must fall back to inference
            FiveLayerBacktestEvaluation(
                signal_family="entry",
                eval_status="evaluated",
                suppression_reason=None,
            ),
        ]

        baseline = _build_run_sample_baseline(raw_candidate_count=4, evaluations=evaluations)

        self.assertEqual(baseline["error_sample_count"], 1)
        self.assertEqual(baseline["aggregatable_sample_count"], 0)
        self.assertEqual(
            baseline["suppressed_reasons"],
            {
                "exec_not_filled:limit_up_blocked": 1,
                "no_forward_bars": 1,
                "exception:RuntimeError": 1,
                "missing_forward_return_5d": 1,  # legacy fallback
            },
        )


@pytest.mark.unit
class TestB1PerBoardLimitPipelineIntegration(unittest.TestCase):
    """B1: end-to-end check that ``InstrumentMaster`` metadata reaches
    ``ExecutionModelResolver`` so per-board limit thresholds are honoured.

    Pre-B1 the pipeline always used the hard-coded ±10 % default, which
    silently blocked 创业板/科创板 high-flyers (+15 ~ +20 %) and inflated
    ``limit_blocked`` rate on the high-volatility cohort the audit report
    flagged. This test class exercises:

      1. 创业板 (300xxx) +18 % candidate must be filled, NOT limit_blocked.
      2. 创业板 (300xxx) +19.95 % candidate must be limit_blocked.
      3. 主板 (60xxxx) +10 % candidate stays limit_blocked (no regression).

    Each test seeds its own InstrumentMaster + ScreeningCandidate +
    StockDaily forward bars, then runs ``run_backtest_pipeline`` and
    inspects the persisted evaluation row.
    """

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test_b1.db")
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

    def _seed_run(
        self,
        *,
        run_id: str,
        code: str,
        name: str,
        is_st: bool = False,
        forward_pct_chg: float,
    ):
        """Seed a one-candidate screening run + matching InstrumentMaster +
        a single forward bar with the requested ``pct_chg``. The bar is
        built as a 一字涨停 (open==high==low==close) so the limit-up
        predicate fires whenever pct_chg crosses the per-board threshold.
        """
        from src.storage import (
            InstrumentMaster,
            ScreeningCandidate,
            ScreeningRun,
            StockDaily,
        )
        with self.db.get_session() as session:
            session.add(InstrumentMaster(
                code=code,
                name=name,
                market="cn",
                exchange="SSE" if code.startswith("6") else "SZSE",
                listing_status="active",
                is_st=is_st,
            ))
            session.add(ScreeningRun(
                run_id=run_id,
                trade_date=date(2024, 1, 15),
                market="cn",
                status="completed",
            ))
            session.flush()
            session.add(ScreeningCandidate(
                run_id=run_id,
                code=code,
                name=name,
                rank=1,
                rule_score=85.0,
                trade_stage="probe_entry",
                setup_type="trend_breakout",
                entry_maturity="high",
                market_regime="balanced",
                theme_position="main_theme",
                candidate_pool_level="leader_pool",
                risk_level="medium",
            ))
            base_close = 100.0
            for i in range(1, 8):
                d = date(2024, 1, 15) + timedelta(days=i)
                if i == 1:
                    # The forward[0] bar — apply the requested pct_chg as
                    # a 一字 candle so limit-up checks are deterministic.
                    price = base_close * (1 + forward_pct_chg / 100)
                    bar = StockDaily(
                        code=code, date=d,
                        open=price, high=price, low=price, close=price,
                        pct_chg=forward_pct_chg,
                        volume=1000.0, amount=price * 1000.0,
                    )
                else:
                    bar = StockDaily(
                        code=code, date=d,
                        open=base_close, high=base_close + 1,
                        low=base_close - 1, close=base_close,
                        pct_chg=0.0,
                        volume=1000.0, amount=base_close * 1000.0,
                    )
                session.add(bar)
            session.commit()

    def _run_pipeline(self, screening_run_id: str):
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.services.backtest_service import FiveLayerBacktestService

        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id=screening_run_id,
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
        )
        evals = EvaluationRepository(self.db).get_by_run(run.backtest_run_id)
        return evals

    def test_chinext_18pct_is_not_limit_blocked_in_pipeline(self):
        """KEY FIX: 创业板 +18 % candidate must be filled in conservative
        execution model, because 创业板 limit is ±20 %.
        """
        self._seed_run(
            run_id="sr-b1-chinext-18",
            code="300999",
            name="测试创业板18",
            forward_pct_chg=18.0,
        )
        evals = self._run_pipeline("sr-b1-chinext-18")
        self.assertEqual(len(evals), 1)
        ev = evals[0]
        self.assertFalse(
            ev.limit_blocked,
            f"创业板 +18 % must NOT be limit_blocked, got "
            f"limit_blocked={ev.limit_blocked}, fill_status={ev.entry_fill_status}",
        )
        self.assertEqual(ev.entry_fill_status, "filled")

    def test_chinext_near_20pct_is_limit_blocked_in_pipeline(self):
        """创业板 +19.95 % is within tolerance of ±20 % → limit_blocked."""
        self._seed_run(
            run_id="sr-b1-chinext-19_95",
            code="300999",
            name="测试创业板19.95",
            forward_pct_chg=19.95,
        )
        evals = self._run_pipeline("sr-b1-chinext-19_95")
        self.assertEqual(len(evals), 1)
        ev = evals[0]
        self.assertTrue(ev.limit_blocked)
        self.assertEqual(ev.entry_fill_status, "limit_blocked")

    def test_main_board_10pct_still_limit_blocked_in_pipeline(self):
        """REGRESSION GUARD: 主板 ±10 % path is unchanged after B1."""
        self._seed_run(
            run_id="sr-b1-main-10",
            code="600519",
            name="测试主板10",
            forward_pct_chg=10.0,
        )
        evals = self._run_pipeline("sr-b1-main-10")
        self.assertEqual(len(evals), 1)
        ev = evals[0]
        self.assertTrue(ev.limit_blocked)
        self.assertEqual(ev.entry_fill_status, "limit_blocked")

    def test_st_5pct_is_limit_blocked_in_pipeline(self):
        """ST candidate +4.95 % within tolerance of ±5 % → limit_blocked."""
        self._seed_run(
            run_id="sr-b1-st-5",
            code="600519",
            name="ST测试",
            is_st=True,
            forward_pct_chg=4.95,
        )
        evals = self._run_pipeline("sr-b1-st-5")
        self.assertEqual(len(evals), 1)
        ev = evals[0]
        self.assertTrue(
            ev.limit_blocked,
            f"ST +4.95 % must be limit_blocked at ±5 %, got "
            f"limit_blocked={ev.limit_blocked}, fill_status={ev.entry_fill_status}",
        )

    def test_pipeline_falls_back_to_main_board_when_instrument_master_missing(self):
        """REGRESSION GUARD: legacy DBs without InstrumentMaster rows must
        still produce sensible results — fall back to ±10 % default. Here
        we seed only the screening data, leaving InstrumentMaster empty.
        """
        from src.storage import (
            ScreeningCandidate,
            ScreeningRun,
            StockDaily,
        )
        run_id = "sr-b1-no-master"
        with self.db.get_session() as session:
            session.add(ScreeningRun(
                run_id=run_id,
                trade_date=date(2024, 1, 15),
                market="cn",
                status="completed",
            ))
            session.flush()
            session.add(ScreeningCandidate(
                run_id=run_id, code="999998", name="No Master",
                rank=1, rule_score=80.0,
                trade_stage="probe_entry", setup_type="trend_breakout",
                entry_maturity="high", market_regime="balanced",
                theme_position="main_theme", candidate_pool_level="leader_pool",
                risk_level="medium",
            ))
            for i in range(1, 8):
                d = date(2024, 1, 15) + timedelta(days=i)
                pct = 10.0 if i == 1 else 0.0
                price = 100.0 * (1 + pct / 100) if i == 1 else 100.0
                session.add(StockDaily(
                    code="999998", date=d,
                    open=price, high=price, low=price, close=price,
                    pct_chg=pct,
                    volume=1000.0, amount=price * 1000.0,
                ))
            session.commit()

        evals = self._run_pipeline(run_id)
        self.assertEqual(len(evals), 1)
        ev = evals[0]
        # Fell back to ±10 %, so +10 % is still limit_blocked
        self.assertTrue(ev.limit_blocked)


@pytest.mark.unit
class TestB5ForwardWindowQualityPipelineIntegration(unittest.TestCase):
    """B5: end-to-end check that ``ForwardBarsMeta`` reaches
    ``_process_candidate`` and that suspended-trading / under-resourced
    samples are suppressed with the right reason instead of silently
    producing a bogus forward_return_5d.
    """

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test_b5.db")
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
        os.environ.pop("BACKTEST_FORWARD_GAP_CHECK_ENABLED", None)
        os.environ.pop("BACKTEST_FORWARD_GAP_TOLERANCE_FACTOR", None)
        self._temp_dir.cleanup()

    def _seed(self, *, run_id, code, name, forward_bar_dates):
        """Seed a one-candidate screening run + a custom forward-bar
        timeline so the test can simulate halts (gap_too_long), missing
        bars (insufficient_bars), or healthy windows.
        """
        from src.storage import (
            ScreeningCandidate,
            ScreeningRun,
            StockDaily,
        )
        with self.db.get_session() as session:
            session.add(ScreeningRun(
                run_id=run_id,
                trade_date=date(2024, 1, 15),
                market="cn",
                status="completed",
            ))
            session.flush()
            session.add(ScreeningCandidate(
                run_id=run_id, code=code, name=name,
                rank=1, rule_score=85.0,
                trade_stage="probe_entry", setup_type="trend_breakout",
                entry_maturity="high", market_regime="balanced",
                theme_position="main_theme", candidate_pool_level="leader_pool",
                risk_level="medium",
            ))
            for d in forward_bar_dates:
                session.add(StockDaily(
                    code=code, date=d,
                    open=100.0, high=101.0, low=99.0, close=100.5,
                    pct_chg=0.5, volume=1000.0, amount=100000.0,
                ))
            session.commit()

    def _run(self, run_id, eval_window_days=5):
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.services.backtest_service import FiveLayerBacktestService

        svc = FiveLayerBacktestService(db_manager=self.db)
        run = svc.run_backtest_pipeline(
            screening_run_id=run_id,
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
            eval_window_days=eval_window_days,
        )
        evals = EvaluationRepository(self.db).get_by_run(run.backtest_run_id)
        return run, evals

    def test_suspended_trading_window_is_suppressed_as_gap_too_long(self):
        """KEY FIX: 5 forward bars actually spanning 60 calendar days
        (typical halt) must NOT contribute a forward_return_5d to the
        aggregator — the row must be suppressed with reason="gap_too_long"
        so analysts can locate halt-driven distortions.
        """
        self._seed(
            run_id="sr-b5-halt",
            code="000333",
            name="测试停牌",
            # 5 bars but spanning 1月16 ~ 3月14 (~58 days)
            forward_bar_dates=[
                date(2024, 1, 16), date(2024, 1, 17),
                date(2024, 3, 12), date(2024, 3, 13), date(2024, 3, 14),
            ],
        )
        _, evals = self._run("sr-b5-halt")
        self.assertEqual(len(evals), 1)
        ev = evals[0]
        self.assertEqual(ev.eval_status, "suppressed")
        self.assertEqual(ev.suppression_reason, "gap_too_long")
        # Forward return must NOT have been produced
        self.assertIsNone(ev.forward_return_5d)
        # And forward_window meta must be persisted for audit
        meta = json.loads(ev.metrics_json)["forward_window"]
        self.assertTrue(meta["gap_too_long"])
        self.assertEqual(meta["actual_bar_count"], 5)
        self.assertGreater(meta["actual_span_days"], meta["gap_threshold_days"])

    def test_underresourced_window_is_suppressed_as_insufficient(self):
        """Only 2 bars come back for a 5d ask → suppression_reason must
        be ``insufficient_forward_bars`` (NOT gap_too_long, because the
        2 bars span only 1 day).
        """
        self._seed(
            run_id="sr-b5-thin",
            code="000222",
            name="测试不足",
            forward_bar_dates=[date(2024, 1, 16), date(2024, 1, 17)],
        )
        _, evals = self._run("sr-b5-thin")
        self.assertEqual(len(evals), 1)
        ev = evals[0]
        self.assertEqual(ev.eval_status, "suppressed")
        self.assertEqual(ev.suppression_reason, "insufficient_forward_bars")
        self.assertIsNone(ev.forward_return_5d)

    def test_healthy_window_is_not_suppressed(self):
        """REGRESSION GUARD: a normal 5-day window (Mon-Fri spanning ~4
        calendar days) must evaluate normally and produce a
        forward_return_5d.
        """
        self._seed(
            run_id="sr-b5-healthy",
            code="000111",
            name="测试健康",
            forward_bar_dates=[date(2024, 1, 16) + timedelta(days=i) for i in range(5)],
        )
        _, evals = self._run("sr-b5-healthy")
        self.assertEqual(len(evals), 1)
        ev = evals[0]
        self.assertNotEqual(ev.eval_status, "suppressed")
        # forward_window meta must still be present for audit
        meta = json.loads(ev.metrics_json)["forward_window"]
        self.assertFalse(meta["gap_too_long"])
        self.assertFalse(meta["insufficient_bars"])

    def test_gap_check_can_be_disabled_via_env(self):
        """ESCAPE HATCH: setting BACKTEST_FORWARD_GAP_CHECK_ENABLED=false
        must restore the legacy behaviour (suspended-trading samples are
        kept, forward_return_5d is computed even though it spans a halt).
        """
        os.environ["BACKTEST_FORWARD_GAP_CHECK_ENABLED"] = "false"
        self._seed(
            run_id="sr-b5-disabled",
            code="000444",
            name="测试关闭",
            forward_bar_dates=[
                date(2024, 1, 16), date(2024, 1, 17),
                date(2024, 3, 12), date(2024, 3, 13), date(2024, 3, 14),
            ],
        )
        _, evals = self._run("sr-b5-disabled")
        self.assertEqual(len(evals), 1)
        ev = evals[0]
        # No suppression reason from B5 — entry evaluator handles the row
        self.assertNotEqual(ev.suppression_reason, "gap_too_long")
        # forward_window meta still persisted (the meta is informational
        # regardless of whether the check is enabled)
        meta = json.loads(ev.metrics_json)["forward_window"]
        self.assertTrue(meta["gap_too_long"])

    def test_custom_tolerance_factor_can_tighten_gap_threshold(self):
        """Setting BACKTEST_FORWARD_GAP_TOLERANCE_FACTOR=1.2 on a 5d
        window puts the cutoff at 6 calendar days; a 5-bar window spanning
        7 calendar days then trips gap_too_long.
        """
        os.environ["BACKTEST_FORWARD_GAP_TOLERANCE_FACTOR"] = "1.2"
        self._seed(
            run_id="sr-b5-tight",
            code="000555",
            name="测试紧阈值",
            forward_bar_dates=[
                date(2024, 1, 16), date(2024, 1, 17), date(2024, 1, 18),
                date(2024, 1, 19), date(2024, 1, 23),  # spans 7 days
            ],
        )
        _, evals = self._run("sr-b5-tight")
        self.assertEqual(len(evals), 1)
        ev = evals[0]
        self.assertEqual(ev.eval_status, "suppressed")
        self.assertEqual(ev.suppression_reason, "gap_too_long")
        meta = json.loads(ev.metrics_json)["forward_window"]
        self.assertEqual(meta["gap_threshold_days"], 6)
        self.assertEqual(meta["actual_span_days"], 7)

    def test_run_sample_baseline_groups_b5_reasons(self):
        """B5's new suppression_reasons (gap_too_long /
        insufficient_forward_bars) must surface as their own buckets in
        ``run.config_json.sample_baseline.suppressed_reasons`` so the
        front-end histogram can show them distinctly from legacy
        missing_forward_return_5d.
        """
        # Two candidates: one halted (gap_too_long), one thin (insufficient).
        from src.storage import (
            ScreeningCandidate,
            ScreeningRun,
            StockDaily,
        )
        run_id = "sr-b5-baseline"
        with self.db.get_session() as session:
            session.add(ScreeningRun(
                run_id=run_id,
                trade_date=date(2024, 1, 15),
                market="cn",
                status="completed",
            ))
            session.flush()
            for code, name in [("000666", "halted"), ("000777", "thin")]:
                session.add(ScreeningCandidate(
                    run_id=run_id, code=code, name=name,
                    rank=1, rule_score=80.0,
                    trade_stage="probe_entry", setup_type="trend_breakout",
                    entry_maturity="high", market_regime="balanced",
                    theme_position="main_theme",
                    candidate_pool_level="leader_pool", risk_level="medium",
                ))
            # Halted: 5 bars spanning 60 days
            for d in [date(2024, 1, 16), date(2024, 1, 17),
                      date(2024, 3, 12), date(2024, 3, 13), date(2024, 3, 14)]:
                session.add(StockDaily(
                    code="000666", date=d,
                    open=100.0, high=101.0, low=99.0, close=100.5,
                    pct_chg=0.5, volume=1000.0, amount=100000.0,
                ))
            # Thin: 2 bars
            for d in [date(2024, 1, 16), date(2024, 1, 17)]:
                session.add(StockDaily(
                    code="000777", date=d,
                    open=100.0, high=101.0, low=99.0, close=100.5,
                    pct_chg=0.5, volume=1000.0, amount=100000.0,
                ))
            session.commit()

        run, _ = self._run(run_id)
        config = json.loads(run.config_json)
        baseline = config["sample_baseline"]
        self.assertEqual(baseline["suppressed_sample_count"], 2)
        self.assertEqual(baseline["suppressed_reasons"]["gap_too_long"], 1)
        self.assertEqual(
            baseline["suppressed_reasons"]["insufficient_forward_bars"], 1,
        )


if __name__ == "__main__":
    unittest.main()
