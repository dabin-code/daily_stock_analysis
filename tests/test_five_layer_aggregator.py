# -*- coding: utf-8 -*-
"""Tests for GroupSummaryAggregator behavior.

Aggregates evaluations into group summaries by:
- overall
- signal_family (entry/observation)
- snapshot_setup_type
- snapshot_market_regime
- combo (theme_position+setup_type / candidate_pool_level+entry_maturity)
- strategy_cohort (primary_strategy+sample_bucket+snapshot context)
"""

import itertools
import json
import os
import tempfile
import unittest
from datetime import date, timedelta

import pytest


_EVAL_SEQUENCE = itertools.count(1000)


def _make_eval(
    signal_family: str = "entry",
    forward_return_5d=None,
    risk_avoided_pct=None,
    outcome: str = "win",
    snapshot_trade_stage: str = "probe_entry",
    stage_success=None,
):
    """Build a real evaluation ORM row so it can be persisted by the repository.

    Deliberately not a MagicMock: ``EvaluationRepository.save_batch`` needs a
    mapped instance, and the aggregator is exercised through the persisted
    round-trip.
    """
    from src.backtest.models.backtest_models import FiveLayerBacktestEvaluation

    seq = next(_EVAL_SEQUENCE)
    return FiveLayerBacktestEvaluation(
        screening_candidate_id=seq,
        trade_date=date(2024, 5, 1) + timedelta(days=seq % 20),
        code=f"9{seq:05d}",
        signal_family=signal_family,
        evaluator_type=signal_family,
        snapshot_trade_stage=snapshot_trade_stage,
        forward_return_5d=forward_return_5d,
        risk_avoided_pct=risk_avoided_pct,
        stage_success=stage_success,
        outcome=outcome,
        eval_status="evaluated",
    )


@pytest.mark.unit
class TestGroupSummaryAggregator(unittest.TestCase):

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test_agg.db")
        os.environ["DATABASE_PATH"] = self._db_path
        from src.config import Config
        Config._instance = None
        from src.storage import DatabaseManager
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()
        self._seed()

    def tearDown(self):
        from src.storage import DatabaseManager
        from src.config import Config
        DatabaseManager.reset_instance()
        Config._instance = None
        os.environ.pop("DATABASE_PATH", None)
        self._temp_dir.cleanup()

    def _seed(self):
        from src.backtest.repositories.run_repo import RunRepository
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.models.backtest_models import FiveLayerBacktestEvaluation

        RunRepository(self.db).create_run(
            backtest_run_id="run-agg",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
            trade_date_from=date(2024, 1, 1),
            trade_date_to=date(2024, 1, 31),
            market="cn",
        )

        evals = [
            # 2 entry signals
            FiveLayerBacktestEvaluation(
                backtest_run_id="run-agg",
                screening_candidate_id=1,
                trade_date=date(2024, 1, 15),
                code="600519",
                signal_family="entry",
                evaluator_type="entry",
                snapshot_trade_stage="probe_entry",
                snapshot_setup_type="trend_breakout",
                snapshot_market_regime="balanced",
                snapshot_theme_position="leader",
                snapshot_candidate_pool_level="tier1",
                snapshot_entry_maturity="high",
                metrics_json=json.dumps({"sample_bucket": "core"}, ensure_ascii=False),
                evidence_json=json.dumps({"primary_strategy": "trend_breakout"}, ensure_ascii=False),
                forward_return_5d=5.0,
                forward_return_1d=2.0,
                mae=-3.0,
                mfe=8.0,
                max_drawdown_from_peak=-4.0,
                plan_success=True,
                signal_quality_score=0.7,
                outcome="win",
                eval_status="evaluated",
            ),
            FiveLayerBacktestEvaluation(
                backtest_run_id="run-agg",
                screening_candidate_id=2,
                trade_date=date(2024, 1, 16),
                code="000858",
                signal_family="entry",
                evaluator_type="entry",
                snapshot_trade_stage="probe_entry",
                snapshot_setup_type="trend_pullback",
                snapshot_market_regime="balanced",
                snapshot_theme_position="rotation",
                snapshot_candidate_pool_level="tier2",
                snapshot_entry_maturity="medium",
                metrics_json=json.dumps({"sample_bucket": "boundary"}, ensure_ascii=False),
                evidence_json=json.dumps({"primary_strategy": "trend_pullback"}, ensure_ascii=False),
                forward_return_5d=-2.0,
                forward_return_1d=-1.0,
                mae=-6.0,
                mfe=3.0,
                max_drawdown_from_peak=-7.0,
                plan_success=False,
                signal_quality_score=0.3,
                outcome="loss",
                eval_status="evaluated",
            ),
            # 2 observation signals
            FiveLayerBacktestEvaluation(
                backtest_run_id="run-agg",
                screening_candidate_id=3,
                trade_date=date(2024, 1, 15),
                code="601318",
                signal_family="observation",
                evaluator_type="observation",
                snapshot_trade_stage="watch",
                snapshot_setup_type=None,
                snapshot_market_regime="balanced",
                snapshot_candidate_pool_level="tier2",
                snapshot_entry_maturity="medium",
                metrics_json=json.dumps({"sample_bucket": "boundary"}, ensure_ascii=False),
                evidence_json=json.dumps({"primary_strategy": "trend_pullback"}, ensure_ascii=False),
                risk_avoided_pct=8.0,
                opportunity_cost_pct=3.0,
                stage_success=True,
                outcome="correct_wait",
                eval_status="evaluated",
            ),
            FiveLayerBacktestEvaluation(
                backtest_run_id="run-agg",
                screening_candidate_id=4,
                trade_date=date(2024, 1, 16),
                code="600036",
                signal_family="observation",
                evaluator_type="observation",
                snapshot_trade_stage="stand_aside",
                snapshot_setup_type=None,
                snapshot_market_regime="defensive",
                snapshot_candidate_pool_level="tier3",
                snapshot_entry_maturity="low",
                metrics_json=json.dumps({"sample_bucket": "noise"}, ensure_ascii=False),
                evidence_json=json.dumps({"primary_strategy": "failed_breakout"}, ensure_ascii=False),
                risk_avoided_pct=2.0,
                opportunity_cost_pct=10.0,
                stage_success=False,
                outcome="missed_opportunity",
                eval_status="evaluated",
            ),
        ]
        EvaluationRepository(self.db).save_batch(evals)

    def _seed_p0_strategy_run(self):
        from src.backtest.models.backtest_models import FiveLayerBacktestEvaluation
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.run_repo import RunRepository

        RunRepository(self.db).create_run(
            backtest_run_id="run-p0-agg",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
            trade_date_from=date(2024, 2, 1),
            trade_date_to=date(2024, 2, 29),
            market="cn",
        )

        evals = [
            FiveLayerBacktestEvaluation(
                backtest_run_id="run-p0-agg",
                screening_candidate_id=11,
                trade_date=date(2024, 2, 20),
                code="300750",
                signal_family="entry",
                evaluator_type="entry",
                snapshot_trade_stage="probe_entry",
                snapshot_setup_type="bottom_divergence_breakout",
                snapshot_market_regime="balanced",
                snapshot_theme_position="main_theme",
                snapshot_candidate_pool_level="leader_pool",
                snapshot_entry_maturity="high",
                metrics_json=json.dumps({"sample_bucket": "core"}, ensure_ascii=False),
                evidence_json=json.dumps(
                    {"primary_strategy": "bottom_divergence_double_breakout"},
                    ensure_ascii=False,
                ),
                forward_return_5d=6.0,
                forward_return_1d=2.5,
                mae=-2.0,
                mfe=9.0,
                outcome="win",
                eval_status="evaluated",
            ),
            FiveLayerBacktestEvaluation(
                backtest_run_id="run-p0-agg",
                screening_candidate_id=12,
                trade_date=date(2024, 2, 21),
                code="002594",
                signal_family="entry",
                evaluator_type="entry",
                snapshot_trade_stage="probe_entry",
                snapshot_setup_type="low123_breakout",
                snapshot_market_regime="balanced",
                snapshot_theme_position="main_theme",
                snapshot_candidate_pool_level="leader_pool",
                snapshot_entry_maturity="high",
                metrics_json=json.dumps({"sample_bucket": "core"}, ensure_ascii=False),
                evidence_json=json.dumps(
                    {"primary_strategy": "ma100_low123_combined"},
                    ensure_ascii=False,
                ),
                forward_return_5d=4.0,
                forward_return_1d=1.5,
                mae=-2.5,
                mfe=7.0,
                outcome="win",
                eval_status="evaluated",
            ),
        ]
        EvaluationRepository(self.db).save_batch(evals)

    def _seed_missing_metric_run(self):
        from src.backtest.models.backtest_models import FiveLayerBacktestEvaluation
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.run_repo import RunRepository

        RunRepository(self.db).create_run(
            backtest_run_id="run-missing-metrics",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
            trade_date_from=date(2024, 3, 1),
            trade_date_to=date(2024, 3, 31),
            market="cn",
        )

        evals = [
            FiveLayerBacktestEvaluation(
                backtest_run_id="run-missing-metrics",
                screening_candidate_id=21,
                trade_date=date(2024, 3, 20),
                code="300001",
                signal_family="entry",
                evaluator_type="entry",
                snapshot_trade_stage="probe_entry",
                snapshot_setup_type="trend_pullback",
                snapshot_market_regime="balanced",
                snapshot_theme_position="main_theme",
                snapshot_candidate_pool_level="leader_pool",
                snapshot_entry_maturity="high",
                metrics_json=json.dumps({"sample_bucket": "core"}, ensure_ascii=False),
                evidence_json=json.dumps({"primary_strategy": "trend_pullback"}, ensure_ascii=False),
                outcome=None,
                eval_status="evaluated",
            ),
            FiveLayerBacktestEvaluation(
                backtest_run_id="run-missing-metrics",
                screening_candidate_id=22,
                trade_date=date(2024, 3, 21),
                code="300002",
                signal_family="observation",
                evaluator_type="observation",
                snapshot_trade_stage="watch",
                snapshot_setup_type="trend_pullback",
                snapshot_market_regime="balanced",
                snapshot_theme_position="main_theme",
                snapshot_candidate_pool_level="leader_pool",
                snapshot_entry_maturity="medium",
                metrics_json=json.dumps({"sample_bucket": "boundary"}, ensure_ascii=False),
                evidence_json=json.dumps({"primary_strategy": "trend_pullback"}, ensure_ascii=False),
                outcome=None,
                eval_status="evaluated",
            ),
        ]
        EvaluationRepository(self.db).save_batch(evals)

    def test_compute_all_returns_summaries(self):
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository
        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-agg")
        self.assertGreater(len(summaries), 0)

    def test_overall_summary(self):
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository
        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-agg")
        overall = [s for s in summaries if s.group_type == "overall"]
        self.assertEqual(len(overall), 1)
        self.assertEqual(overall[0].sample_count, 4)

    def test_signal_family_groups(self):
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository
        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-agg")
        sf = {s.group_key: s for s in summaries if s.group_type == "signal_family"}
        self.assertIn("entry", sf)
        self.assertIn("observation", sf)
        self.assertEqual(sf["entry"].sample_count, 2)
        self.assertEqual(sf["observation"].sample_count, 2)

    def test_entry_win_rate(self):
        """Entry group should have 50% win rate (1 win, 1 loss)."""
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository
        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-agg")
        entry = next(s for s in summaries if s.group_type == "signal_family" and s.group_key == "entry")
        self.assertAlmostEqual(entry.win_rate_pct, 50.0)

    def test_entry_avg_return(self):
        """Entry avg return = (5.0 + -2.0) / 2 = 1.5."""
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository
        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-agg")
        entry = next(s for s in summaries if s.group_type == "signal_family" and s.group_key == "entry")
        self.assertAlmostEqual(entry.avg_return_pct, 1.5)

    def test_entry_avg_return_prefers_real_trade_return_when_available(self):
        """Structured replay rows should aggregate actual round-trip PnL."""
        from src.backtest.aggregators.group_summary_aggregator import aggregate_group
        from src.backtest.models.backtest_models import FiveLayerBacktestEvaluation

        metrics = aggregate_group(
            [
                FiveLayerBacktestEvaluation(
                    backtest_run_id="run-replay-agg",
                    trade_date=date(2024, 1, 15),
                    code="600001",
                    signal_family="entry",
                    evaluator_type="entry",
                    eval_status="evaluated",
                    forward_return_5d=1.0,
                    trade_return_pct=8.0,
                    outcome="win",
                ),
                FiveLayerBacktestEvaluation(
                    backtest_run_id="run-replay-agg",
                    trade_date=date(2024, 1, 16),
                    code="600002",
                    signal_family="entry",
                    evaluator_type="entry",
                    eval_status="evaluated",
                    forward_return_5d=2.0,
                    trade_return_pct=-4.0,
                    outcome="loss",
                ),
            ],
            family_filter="entry",
        )

        self.assertAlmostEqual(metrics["avg_return_pct"], 2.0)
        self.assertAlmostEqual(metrics["profit_factor"], 2.0)

    def test_setup_type_groups(self):
        """Should have groups for non-None setup_types."""
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository
        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-agg")
        st = {s.group_key: s for s in summaries if s.group_type == "setup_type"}
        self.assertIn("trend_breakout", st)
        self.assertIn("trend_pullback", st)

    def test_market_regime_groups(self):
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository
        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-agg")
        mr = {s.group_key: s for s in summaries if s.group_type == "market_regime"}
        self.assertIn("balanced", mr)
        self.assertIn("defensive", mr)

    def test_combo_groups(self):
        """Should produce non-family combo summaries for configured dimension pairs."""
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository
        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-agg")
        combos = [s for s in summaries if s.group_type == "combo"]
        self.assertGreater(len(combos), 0)
        keys = [s.group_key for s in combos]
        self.assertIn(
            "snapshot_theme_position=leader|snapshot_setup_type=trend_breakout",
            keys,
        )

    def test_combo_groups_skip_market_regime_signal_family_duplicates(self):
        """Combo summaries should not duplicate dedicated market_regime_signal_family rows."""
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository

        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-agg")
        combo_keys = [s.group_key for s in summaries if s.group_type == "combo"]

        self.assertNotIn("balanced+entry", combo_keys)

    def test_summaries_persisted(self):
        """Summaries should be saved to DB."""
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository
        sr = SummaryRepository(self.db)
        agg = GroupSummaryAggregator(EvaluationRepository(self.db), sr)
        agg.compute_all_summaries("run-agg")
        persisted = sr.get_by_run("run-agg")
        self.assertGreater(len(persisted), 0)

    def test_observation_stage_success_rate(self):
        """Observation group: 1 correct_wait, 1 missed_opportunity → 50% stage_success."""
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository
        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-agg")
        obs = next(s for s in summaries if s.group_type == "signal_family" and s.group_key == "observation")
        self.assertAlmostEqual(obs.win_rate_pct, 50.0)


    def test_entry_group_exposes_new_metrics(self):
        """Entry group should include profit factor, streak and execution metrics."""
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository
        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-agg")
        entry = next(s for s in summaries if s.group_type == "signal_family" and s.group_key == "entry")

        self.assertAlmostEqual(entry.profit_factor, 2.5)
        self.assertIsNone(entry.avg_holding_days)
        self.assertEqual(entry.max_consecutive_losses, 1)
        self.assertAlmostEqual(entry.plan_execution_rate, 0.5)
        self.assertAlmostEqual(entry.stage_accuracy_rate, 0.5)

    def test_overall_summary_stage_accuracy_uses_entry_and_observation_rules(self):
        """Stage accuracy applies the entry rule (profitable) at the top level and
        keeps the observation rule (``stage_success``) in ``family_breakdown``.

        The top-level column is entry-only like every other metric on a mixed
        row; the per-family rules stay readable through the breakdown.
        """
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository
        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-agg")
        overall = next(s for s in summaries if s.group_type == "overall" and s.group_key == "all")

        # entry: +5.0 profitable, -2.0 not → 0.5
        self.assertAlmostEqual(overall.stage_accuracy_rate, 0.5)

        metrics = json.loads(overall.metrics_json)
        self.assertAlmostEqual(metrics["family_breakdown"]["entry"]["stage_accuracy_rate"], 0.5)
        # observation: one stage_success=True, one False → 0.5
        self.assertAlmostEqual(metrics["family_breakdown"]["observation"]["stage_accuracy_rate"], 0.5)

    def test_market_regime_signal_family_split_summaries(self):
        """Single-dimension summaries should also emit signal-family split rows."""
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository

        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-agg")
        split = {
            s.group_key: s
            for s in summaries
            if s.group_type == "market_regime_signal_family"
        }

        balanced_entry = "snapshot_market_regime=balanced|signal_family=entry"
        balanced_observation = "snapshot_market_regime=balanced|signal_family=observation"
        defensive_observation = "snapshot_market_regime=defensive|signal_family=observation"
        self.assertIn(balanced_entry, split)
        self.assertIn(balanced_observation, split)
        self.assertIn(defensive_observation, split)
        self.assertEqual(split[balanced_entry].sample_count, 2)
        self.assertAlmostEqual(split[balanced_observation].avg_return_pct, 8.0)

    def test_overall_summary_metrics_include_family_breakdown(self):
        """Mixed-family overall summary should persist a family breakdown in metrics_json."""
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository

        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-agg")
        overall = next(s for s in summaries if s.group_type == "overall" and s.group_key == "all")

        self.assertIsNotNone(overall.metrics_json)
        metrics = json.loads(overall.metrics_json)
        self.assertEqual(metrics["family_breakdown"]["entry"]["sample_count"], 2)
        self.assertAlmostEqual(metrics["family_breakdown"]["entry"]["avg_return_pct"], 1.5)
        self.assertEqual(metrics["family_breakdown"]["observation"]["sample_count"], 2)
        self.assertAlmostEqual(metrics["family_breakdown"]["observation"]["avg_return_pct"], 5.0)

    def test_signal_family_summary_metrics_skip_nested_family_breakdown(self):
        """Single-family summaries should not carry redundant family_breakdown payloads."""
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository

        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-agg")
        entry = next(s for s in summaries if s.group_type == "signal_family" and s.group_key == "entry")

        self.assertIsNotNone(entry.metrics_json)
        metrics = json.loads(entry.metrics_json)
        self.assertNotIn("family_breakdown", metrics)

    def test_setup_type_summary_always_emits_family_breakdown(self):
        """Mixed-capable single-dimension summaries should always carry a
        ``family_breakdown`` so consumers can read family-correct metrics
        without falling back to legacy mixed fields.

        Regression guard for C3/C4/C5: even when a setup_type bucket happens
        to contain a single family, the breakdown must be present so the API
        contract is uniform.
        """
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository

        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-agg")
        trend_breakout = next(
            s for s in summaries
            if s.group_type == "setup_type" and s.group_key == "trend_breakout"
        )

        metrics = json.loads(trend_breakout.metrics_json)
        self.assertIn("family_breakdown", metrics)
        self.assertIn("entry", metrics["family_breakdown"])
        self.assertEqual(metrics["family_breakdown"]["entry"]["sample_count"], 1)

    def test_family_breakdown_entry_uses_entry_only_win_rate(self):
        """family_breakdown.entry win_rate must only count entry winners
        (``outcome=='win'``) and ignore observation outcomes.

        Regression guard for C4: legacy mixed win_rate combined ``win`` and
        ``correct_wait`` and was therefore meaningless for entry-quality
        decisions.
        """
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository

        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-agg")
        overall = next(s for s in summaries if s.group_type == "overall" and s.group_key == "all")

        metrics = json.loads(overall.metrics_json)
        # Seeded entries: one win + one loss → 50% entry win_rate
        self.assertAlmostEqual(metrics["family_breakdown"]["entry"]["win_rate_pct"], 50.0)
        # Seeded observations: one correct_wait + one missed_opportunity
        # → 50% correct-wait rate, NOT mixed with entry's "win" outcomes
        self.assertAlmostEqual(metrics["family_breakdown"]["observation"]["win_rate_pct"], 50.0)

    def test_family_breakdown_entry_profit_factor_uses_entry_returns_only(self):
        """family_breakdown.entry.profit_factor must use only entry's
        forward_return_5d, never observation's risk_avoided_pct.

        Regression guard for C5: profit_factor only makes sense for realised
        directional pnl; mixing observation's "risk avoided" inflates it.
        """
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository

        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-agg")
        overall = next(s for s in summaries if s.group_type == "overall" and s.group_key == "all")

        metrics = json.loads(overall.metrics_json)
        # Seeded entry returns: +5.0 and -2.0 → profit_factor = 5.0/2.0 = 2.5
        self.assertAlmostEqual(metrics["family_breakdown"]["entry"]["profit_factor"], 2.5)

    def test_strategy_cohort_summaries_use_primary_strategy_and_sample_bucket(self):
        """Strategy cohort rows should group by primary strategy, bucket and snapshot context."""
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository

        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-agg")
        cohorts = {
            s.group_key: s
            for s in summaries
            if s.group_type == "strategy_cohort"
        }

        trend_pullback_boundary = "ps=trend_pullback|sb=boundary|mr=balanced|cp=tier2|em=medium"
        self.assertIn(trend_pullback_boundary, cohorts)
        # 整组 2 条（entry −2.0 + observation 8.0），sample_count 描述整组…
        self.assertEqual(cohorts[trend_pullback_boundary].sample_count, 2)
        # …但顶层收益只代表 entry 族，所以是 −2.0 而不是两族混合后的 3.0
        self.assertAlmostEqual(cohorts[trend_pullback_boundary].avg_return_pct, -2.0)

    def test_strategy_cohort_summary_includes_family_breakdown_for_mixed_samples(self):
        """Mixed-family strategy cohort should persist family breakdown in metrics_json."""
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository

        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-agg")
        cohort = next(
            s for s in summaries
            if s.group_type == "strategy_cohort"
            and s.group_key.startswith("ps=trend_pullback|sb=boundary|")
        )

        self.assertIsNotNone(cohort.metrics_json)
        metrics = json.loads(cohort.metrics_json)
        self.assertEqual(
            metrics["strategy_cohort_context"],
            {
                "primary_strategy": "trend_pullback",
                "sample_bucket": "boundary",
                "snapshot_market_regime": "balanced",
                "snapshot_candidate_pool_level": "tier2",
                "snapshot_entry_maturity": "medium",
            },
        )
        self.assertEqual(metrics["family_breakdown"]["entry"]["sample_count"], 1)
        self.assertAlmostEqual(metrics["family_breakdown"]["entry"]["avg_return_pct"], -2.0)
        self.assertEqual(metrics["family_breakdown"]["observation"]["sample_count"], 1)
        self.assertAlmostEqual(metrics["family_breakdown"]["observation"]["avg_return_pct"], 8.0)

    def test_strategy_cohort_key_stays_within_group_key_limit(self):
        """Long strategy ids should still produce a short stable cohort key."""
        from src.backtest.aggregators.group_summary_aggregator import (
            _build_strategy_cohort_key,
        )

        key = _build_strategy_cohort_key(
            primary_strategy="trend_pullback_super_long_strategy_name_for_length_guardrail_test",
            sample_bucket="boundary",
            cohort_values={
                "snapshot_market_regime": "balanced_market_regime_name",
                "snapshot_candidate_pool_level": "tier2_candidate_pool",
                "snapshot_entry_maturity": "medium_entry_maturity",
            },
        )

        self.assertLessEqual(len(key), 128)
        self.assertIn("h=", key)

    def test_strategy_cohort_skips_blank_primary_strategy(self):
        """Blank primary_strategy should not form a ps= cohort bucket."""
        from src.backtest.aggregators.group_summary_aggregator import _group_by_strategy_cohort
        from src.backtest.models.backtest_models import FiveLayerBacktestEvaluation

        evaluations = [
            FiveLayerBacktestEvaluation(
                backtest_run_id="run-agg",
                screening_candidate_id=99,
                code="300750",
                signal_family="entry",
                snapshot_market_regime="balanced",
                snapshot_candidate_pool_level="tier1",
                snapshot_entry_maturity="high",
                metrics_json=json.dumps({"sample_bucket": "core"}, ensure_ascii=False),
                evidence_json=json.dumps({"primary_strategy": "   "}, ensure_ascii=False),
                forward_return_5d=2.0,
                outcome="win",
            ),
        ]

        groups = _group_by_strategy_cohort(evaluations)
        self.assertEqual(groups, {})

    def test_p0_strategy_cohorts_include_bottom_divergence_and_ma100_low123(self):
        """P0 strategy cohorts should preserve bottom divergence and MA100+Low123 attribution."""
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository

        self._seed_p0_strategy_run()

        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-p0-agg")
        cohorts = {
            s.group_key: s
            for s in summaries
            if s.group_type == "strategy_cohort"
        }

        self.assertIn(
            "ps=bottom_divergence_double_breakout|sb=core|mr=balanced|cp=leader_pool|em=high",
            cohorts,
        )
        self.assertIn(
            "ps=ma100_low123_combined|sb=core|mr=balanced|cp=leader_pool|em=high",
            cohorts,
        )
        self.assertEqual(
            cohorts["ps=bottom_divergence_double_breakout|sb=core|mr=balanced|cp=leader_pool|em=high"].sample_count,
            1,
        )
        self.assertAlmostEqual(
            cohorts["ps=bottom_divergence_double_breakout|sb=core|mr=balanced|cp=leader_pool|em=high"].avg_return_pct,
            6.0,
        )
        self.assertEqual(
            cohorts["ps=ma100_low123_combined|sb=core|mr=balanced|cp=leader_pool|em=high"].sample_count,
            1,
        )
        self.assertAlmostEqual(
            cohorts["ps=ma100_low123_combined|sb=core|mr=balanced|cp=leader_pool|em=high"].avg_return_pct,
            4.0,
        )

    def test_p0_setup_type_summaries_include_bottom_divergence_and_low123(self):
        """P0 setup summaries should expose bottom divergence and low123 setup types."""
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository

        self._seed_p0_strategy_run()

        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-p0-agg")
        setup_groups = {
            s.group_key: s
            for s in summaries
            if s.group_type == "setup_type"
        }

        self.assertIn("bottom_divergence_breakout", setup_groups)
        self.assertIn("low123_breakout", setup_groups)
        self.assertEqual(setup_groups["bottom_divergence_breakout"].sample_count, 1)
        self.assertEqual(setup_groups["low123_breakout"].sample_count, 1)

    def test_overall_summary_exposes_raw_and_aggregatable_sample_baseline(self):
        """Overall summary should expose both raw sample count and the subset that contributes to metrics."""
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository

        self._seed_missing_metric_run()

        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-missing-metrics")
        overall = next(s for s in summaries if s.group_type == "overall" and s.group_key == "all")

        self.assertEqual(overall.sample_count, 2)
        self.assertIsNone(overall.avg_return_pct)

        metrics = json.loads(overall.metrics_json)
        self.assertEqual(metrics["sample_baseline"]["raw_sample_count"], 2)
        self.assertEqual(metrics["sample_baseline"]["aggregatable_sample_count"], 0)
        self.assertEqual(metrics["sample_baseline"]["entry_sample_count"], 1)
        self.assertEqual(metrics["sample_baseline"]["observation_sample_count"], 1)
        self.assertEqual(metrics["sample_baseline"]["suppressed_sample_count"], 2)
        self.assertEqual(
            metrics["sample_baseline"]["suppressed_reasons"],
            {
                "missing_forward_return_5d": 1,
                "missing_risk_avoided_pct": 1,
            },
        )

    def test_setup_type_summary_keeps_missing_metric_strategy_with_suppressed_reason(self):
        """Setup types seen in evaluations should still produce summary rows even when all metric fields are missing."""
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository

        self._seed_missing_metric_run()

        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-missing-metrics")
        setup_groups = {
            s.group_key: s
            for s in summaries
            if s.group_type == "setup_type"
        }

        self.assertIn("trend_pullback", setup_groups)
        trend_pullback = setup_groups["trend_pullback"]
        self.assertEqual(trend_pullback.sample_count, 2)
        self.assertIsNone(trend_pullback.avg_return_pct)

        metrics = json.loads(trend_pullback.metrics_json)
        self.assertEqual(metrics["sample_baseline"]["raw_sample_count"], 2)
        self.assertEqual(metrics["sample_baseline"]["aggregatable_sample_count"], 0)
        self.assertEqual(metrics["sample_baseline"]["suppressed_sample_count"], 2)
        self.assertFalse(metrics["threshold_check"]["can_display"])

    def _persist_and_read(self, group_type: str, group_key: str, evaluations):
        """Persist a fresh run of ``evaluations`` and read one summary back."""
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.run_repo import RunRepository
        from src.backtest.repositories.summary_repo import SummaryRepository

        run_id = f"run-family-{group_type}-{group_key}"
        RunRepository(self.db).create_run(
            backtest_run_id=run_id,
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
            trade_date_from=date(2024, 5, 1),
            trade_date_to=date(2024, 5, 31),
            market="cn",
        )
        for evaluation in evaluations:
            evaluation.backtest_run_id = run_id
        EvaluationRepository(self.db).save_batch(evaluations)

        summary_repo = SummaryRepository(self.db)
        GroupSummaryAggregator(EvaluationRepository(self.db), summary_repo).compute_all_summaries(run_id)
        return next(
            s for s in summary_repo.get_by_run(run_id)
            if s.group_type == group_type and s.group_key == group_key
        )

    def _read_summary_columns(self, run_id: str, group_type: str, group_key: str):
        """Read the persisted summary columns directly, bypassing the ORM path."""
        from sqlalchemy import text

        with self.db.get_session() as session:
            return session.execute(
                text(
                    "SELECT sample_count, avg_return_pct, win_rate_pct, stage_accuracy_rate "
                    "FROM five_layer_backtest_group_summaries "
                    "WHERE backtest_run_id = :run_id AND group_type = :group_type "
                    "AND group_key = :group_key"
                ),
                {"run_id": run_id, "group_type": group_type, "group_key": group_key},
            ).fetchone()

    def test_mixed_group_top_level_metrics_exclude_observation(self) -> None:
        """真正混族的分组，顶层 win_rate / profit_factor 只能来自 entry 族。

        observation 的 risk_avoided_pct 与 entry 的 forward_return 量纲不同，
        且 correct_wait 并进胜率分子会把评级系统性抬高。
        """
        evals = [
            _make_eval(signal_family="entry", forward_return_5d=5.0, outcome="win"),
            _make_eval(signal_family="entry", forward_return_5d=-3.0, outcome="loss"),
            _make_eval(signal_family="observation", risk_avoided_pct=8.0, outcome="correct_wait"),
            _make_eval(signal_family="observation", risk_avoided_pct=6.0, outcome="correct_wait"),
        ]

        summary = self._persist_and_read(group_type="trade_stage", group_key="probe_entry",
                                         evaluations=evals)

        # entry 族 2 条中 1 胜 → 50%；混入两条 correct_wait 会变成 75%
        self.assertAlmostEqual(summary.win_rate_pct, 50.0, places=2)
        self.assertAlmostEqual(summary.avg_return_pct, 1.0, places=2)
        # 顶层收益只代表 entry，但样本数仍须描述整组 4 条，否则读报告的人
        # 会以为样本量不足
        self.assertEqual(summary.sample_count, 4)

        # 直查落库列，确认上面的断言不是读回路径合并出来的
        row = self._read_summary_columns(
            "run-family-trade_stage-probe_entry", "trade_stage", "probe_entry",
        )
        self.assertEqual(row[0], 4)
        self.assertAlmostEqual(row[1], 1.0, places=2)
        self.assertAlmostEqual(row[2], 50.0, places=2)

    def test_single_family_group_keeps_its_own_family_metrics(self) -> None:
        """反例：observation 行不能被 entry 过滤清空。

        没有这条，「顶层只算 entry」的改法会把整类分组行的指标抹成 None。
        """
        evals = [
            _make_eval(signal_family="observation", risk_avoided_pct=8.0, outcome="correct_wait"),
            _make_eval(signal_family="observation", risk_avoided_pct=-2.0, outcome="missed_opportunity"),
        ]

        summary = self._persist_and_read(group_type="signal_family", group_key="observation",
                                         evaluations=evals)

        self.assertAlmostEqual(summary.win_rate_pct, 50.0, places=2)
        self.assertIsNotNone(summary.avg_return_pct)

    def test_mixed_family_aggregation_requires_explicit_opt_in(self) -> None:
        """反例：默认不允许混族。旧口径必须显式声明才能拿到。"""
        from src.backtest.aggregators.group_summary_aggregator import aggregate_group

        with self.assertRaises(ValueError):
            aggregate_group([
                _make_eval(signal_family="entry", forward_return_5d=1.0),
            ])

    def _seed_pattern_code_run(self):
        from src.backtest.models.backtest_models import FiveLayerBacktestEvaluation
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.run_repo import RunRepository

        RunRepository(self.db).create_run(
            backtest_run_id="run-pattern",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
            trade_date_from=date(2024, 4, 1),
            trade_date_to=date(2024, 4, 30),
            market="cn",
        )

        evals = [
            FiveLayerBacktestEvaluation(
                backtest_run_id="run-pattern",
                screening_candidate_id=31,
                trade_date=date(2024, 4, 10),
                code="600519",
                signal_family="entry",
                evaluator_type="entry",
                snapshot_trade_stage="probe_entry",
                snapshot_setup_type="bottom_divergence_breakout",
                snapshot_market_regime="balanced",
                snapshot_theme_position="main_theme",
                snapshot_candidate_pool_level="leader_pool",
                snapshot_entry_maturity="high",
                factor_snapshot_json=json.dumps({
                    "bottom_divergence_pattern_code": "price_down_macd_up",
                    "bottom_divergence_signal_strength": 0.75,
                }, ensure_ascii=False),
                forward_return_5d=6.0,
                mae=-2.0,
                mfe=9.0,
                outcome="win",
                eval_status="evaluated",
            ),
            FiveLayerBacktestEvaluation(
                backtest_run_id="run-pattern",
                screening_candidate_id=32,
                trade_date=date(2024, 4, 11),
                code="000858",
                signal_family="entry",
                evaluator_type="entry",
                snapshot_trade_stage="probe_entry",
                snapshot_setup_type="bottom_divergence_breakout",
                snapshot_market_regime="balanced",
                snapshot_theme_position="main_theme",
                snapshot_candidate_pool_level="leader_pool",
                snapshot_entry_maturity="high",
                factor_snapshot_json=json.dumps({
                    "bottom_divergence_pattern_code": "price_down_macd_up",
                    "bottom_divergence_signal_strength": 0.45,
                }, ensure_ascii=False),
                forward_return_5d=-3.0,
                mae=-5.0,
                mfe=2.0,
                outcome="loss",
                eval_status="evaluated",
            ),
            FiveLayerBacktestEvaluation(
                backtest_run_id="run-pattern",
                screening_candidate_id=33,
                trade_date=date(2024, 4, 12),
                code="601318",
                signal_family="entry",
                evaluator_type="entry",
                snapshot_trade_stage="probe_entry",
                snapshot_setup_type="bottom_divergence_breakout",
                snapshot_market_regime="balanced",
                snapshot_theme_position="main_theme",
                snapshot_candidate_pool_level="leader_pool",
                snapshot_entry_maturity="medium",
                factor_snapshot_json=json.dumps({
                    "bottom_divergence_pattern_code": "price_flat_macd_up",
                    "bottom_divergence_signal_strength": 0.2,
                }, ensure_ascii=False),
                forward_return_5d=1.0,
                mae=-1.0,
                mfe=3.0,
                outcome="win",
                eval_status="evaluated",
            ),
            # No pattern_code — should be excluded from pattern grouping
            FiveLayerBacktestEvaluation(
                backtest_run_id="run-pattern",
                screening_candidate_id=34,
                trade_date=date(2024, 4, 13),
                code="600036",
                signal_family="entry",
                evaluator_type="entry",
                snapshot_trade_stage="probe_entry",
                snapshot_setup_type="trend_breakout",
                snapshot_market_regime="balanced",
                snapshot_theme_position="main_theme",
                snapshot_candidate_pool_level="leader_pool",
                snapshot_entry_maturity="high",
                factor_snapshot_json=json.dumps({}, ensure_ascii=False),
                forward_return_5d=2.0,
                mae=-1.5,
                mfe=4.0,
                outcome="win",
                eval_status="evaluated",
            ),
        ]
        EvaluationRepository(self.db).save_batch(evals)

    def test_pattern_code_grouping(self):
        """Should produce summaries grouped by bottom_divergence_pattern_code."""
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository

        self._seed_pattern_code_run()

        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-pattern")
        pattern_groups = {
            s.group_key: s
            for s in summaries
            if s.group_type == "pattern_code"
        }

        self.assertIn("price_down_macd_up", pattern_groups)
        self.assertIn("price_flat_macd_up", pattern_groups)
        self.assertEqual(pattern_groups["price_down_macd_up"].sample_count, 2)
        self.assertEqual(pattern_groups["price_flat_macd_up"].sample_count, 1)
        self.assertAlmostEqual(pattern_groups["price_down_macd_up"].avg_return_pct, 1.5)

    def test_signal_strength_band_grouping(self):
        """Should produce summaries grouped by signal strength bands (weak/medium/strong)."""
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.summary_repo import SummaryRepository

        self._seed_pattern_code_run()

        agg = GroupSummaryAggregator(EvaluationRepository(self.db), SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-pattern")
        strength_groups = {
            s.group_key: s
            for s in summaries
            if s.group_type == "signal_strength_band"
        }

        self.assertIn("strong", strength_groups)    # 0.75
        self.assertIn("medium", strength_groups)     # 0.45
        self.assertIn("weak", strength_groups)       # 0.2
        self.assertEqual(strength_groups["strong"].sample_count, 1)
        self.assertAlmostEqual(strength_groups["strong"].avg_return_pct, 6.0)
        self.assertEqual(strength_groups["medium"].sample_count, 1)
        self.assertAlmostEqual(strength_groups["medium"].avg_return_pct, -3.0)
        self.assertEqual(strength_groups["weak"].sample_count, 1)
        self.assertAlmostEqual(strength_groups["weak"].avg_return_pct, 1.0)

    def test_no_pattern_code_no_group(self):
        """Evaluations without pattern_code in factor_snapshot should not appear in pattern_code groups."""
        from src.backtest.aggregators.group_summary_aggregator import _group_by_pattern_code
        from src.backtest.models.backtest_models import FiveLayerBacktestEvaluation

        evals = [
            FiveLayerBacktestEvaluation(
                backtest_run_id="run-x",
                code="600036",
                signal_family="entry",
                factor_snapshot_json=json.dumps({"volume_ratio": 2.5}, ensure_ascii=False),
                forward_return_5d=2.0,
                outcome="win",
            ),
        ]
        groups = _group_by_pattern_code(evals)
        self.assertEqual(groups, {})


    def test_ai_override_grouping(self):
        """Should produce summaries grouped by ai_overridden status from metrics_json."""
        from src.backtest.aggregators.group_summary_aggregator import GroupSummaryAggregator
        from src.backtest.models.backtest_models import FiveLayerBacktestEvaluation
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.run_repo import RunRepository
        from src.backtest.repositories.summary_repo import SummaryRepository

        RunRepository(self.db).create_run(
            backtest_run_id="run-ai-override",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
            trade_date_from=date(2024, 4, 1),
            trade_date_to=date(2024, 4, 30),
            market="cn",
        )

        evals = [
            FiveLayerBacktestEvaluation(
                backtest_run_id="run-ai-override",
                screening_candidate_id=41,
                trade_date=date(2024, 4, 10),
                code="600519",
                signal_family="entry",
                evaluator_type="entry",
                metrics_json=json.dumps({"ai_overridden": True}),
                forward_return_5d=8.0,
                mae=-1.5,
                mfe=10.0,
                outcome="win",
                eval_status="evaluated",
            ),
            FiveLayerBacktestEvaluation(
                backtest_run_id="run-ai-override",
                screening_candidate_id=42,
                trade_date=date(2024, 4, 11),
                code="000858",
                signal_family="entry",
                evaluator_type="entry",
                metrics_json=json.dumps({"ai_overridden": True}),
                forward_return_5d=-2.0,
                mae=-4.0,
                mfe=3.0,
                outcome="loss",
                eval_status="evaluated",
            ),
            FiveLayerBacktestEvaluation(
                backtest_run_id="run-ai-override",
                screening_candidate_id=43,
                trade_date=date(2024, 4, 12),
                code="601318",
                signal_family="entry",
                evaluator_type="entry",
                metrics_json=json.dumps({"ai_overridden": False}),
                forward_return_5d=5.0,
                mae=-2.0,
                mfe=7.0,
                outcome="win",
                eval_status="evaluated",
            ),
        ]

        repo = EvaluationRepository(self.db)
        repo.save_batch(evals)

        agg = GroupSummaryAggregator(repo, SummaryRepository(self.db))
        summaries = agg.compute_all_summaries("run-ai-override")
        ai_groups = {
            s.group_key: s
            for s in summaries
            if s.group_type == "ai_override"
        }

        self.assertIn("ai_overridden", ai_groups)
        self.assertIn("ai_not_overridden", ai_groups)
        self.assertEqual(ai_groups["ai_overridden"].sample_count, 2)
        self.assertEqual(ai_groups["ai_not_overridden"].sample_count, 1)
        self.assertAlmostEqual(ai_groups["ai_overridden"].avg_return_pct, 3.0)
        self.assertAlmostEqual(ai_groups["ai_not_overridden"].avg_return_pct, 5.0)


if __name__ == "__main__":
    unittest.main()
