# -*- coding: utf-8 -*-
"""TDD: Tests for RecommendationEngine.

Validates graded recommendation generation:
- observation level (sample >= 5)
- hypothesis level (sample >= 20, stability passed)
- actionable level (sample >= 50, stability + consistency + evidence)
- RED LINE: never modifies rules/thresholds/parameters
"""

import json
import os
import tempfile
import unittest
from datetime import date

import pytest


@pytest.mark.unit
class TestRecommendationEngine(unittest.TestCase):

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test_rec.db")
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
        from src.backtest.repositories.summary_repo import SummaryRepository
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.models.backtest_models import FiveLayerBacktestEvaluation

        RunRepository(self.db).create_run(
            backtest_run_id="run-rec",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
            trade_date_from=date(2024, 1, 1),
            trade_date_to=date(2024, 6, 30),
            market="cn",
        )

        sr = SummaryRepository(self.db)

        # Overall — should be skipped (group_type == "overall")
        sr.upsert_summary("run-rec", "overall", "all", sample_count=100,
                          avg_return_pct=2.0, win_rate_pct=55.0)

        # Strong signal_family=entry: high win_rate + positive return → weight_increase
        # sample=60, stability + consistency → actionable
        sr.upsert_summary("run-rec", "signal_family", "entry", sample_count=60,
                          avg_return_pct=4.5, win_rate_pct=65.0,
                          time_bucket_stability=0.08, extreme_sample_ratio=0.03)

        # Weak setup_type=mean_reversion: low win_rate + negative return → weight_decrease
        # sample=25, stability ok → hypothesis
        sr.upsert_summary("run-rec", "setup_type", "mean_reversion", sample_count=25,
                          avg_return_pct=-3.0, win_rate_pct=30.0,
                          time_bucket_stability=0.10, extreme_sample_ratio=0.05)

        # Small setup_type=event: sample=8 → observation only
        sr.upsert_summary("run-rec", "setup_type", "event", sample_count=8,
                          avg_return_pct=-2.0, win_rate_pct=35.0,
                          time_bucket_stability=0.05, extreme_sample_ratio=0.02)

        # Borderline: positive win_rate but negative return → execution_review
        # sample=30, stability ok → hypothesis
        sr.upsert_summary("run-rec", "market_regime", "volatile", sample_count=30,
                          avg_return_pct=-1.0, win_rate_pct=52.0,
                          time_bucket_stability=0.12, extreme_sample_ratio=0.04)

        # No recommendation: mediocre metrics
        sr.upsert_summary("run-rec", "market_regime", "balanced", sample_count=50,
                          avg_return_pct=1.0, win_rate_pct=50.0,
                          time_bucket_stability=0.10, extreme_sample_ratio=0.05)

        # Tiny group below observation min → should be skipped
        sr.upsert_summary("run-rec", "entry_maturity", "ULTRA", sample_count=3,
                          avg_return_pct=10.0, win_rate_pct=100.0)

        # Seed some evaluations for evidence building
        evals = [
            FiveLayerBacktestEvaluation(
                backtest_run_id="run-rec",
                screening_candidate_id=i,
                trade_date=date(2024, 1, 15),
                code=f"60051{i}",
                signal_family="entry",
                evaluator_type="entry",
                snapshot_setup_type="trend_breakout",
                snapshot_market_regime="balanced",
                forward_return_5d=3.0,
                outcome="win",
                eval_status="evaluated",
            )
            for i in range(1, 6)
        ]
        EvaluationRepository(self.db).save_batch(evals)

    def test_generates_recommendations(self):
        from src.backtest.recommendations.recommendation_engine import RecommendationEngine
        from src.backtest.repositories.summary_repo import SummaryRepository
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.recommendation_repo import RecommendationRepository
        engine = RecommendationEngine(
            SummaryRepository(self.db),
            EvaluationRepository(self.db),
            RecommendationRepository(self.db),
        )
        recs = engine.generate_recommendations("run-rec")
        self.assertGreater(len(recs), 0)

    def test_skips_overall_group(self):
        """Overall summary should never produce a recommendation."""
        from src.backtest.recommendations.recommendation_engine import RecommendationEngine
        from src.backtest.repositories.summary_repo import SummaryRepository
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.recommendation_repo import RecommendationRepository
        engine = RecommendationEngine(
            SummaryRepository(self.db),
            EvaluationRepository(self.db),
            RecommendationRepository(self.db),
        )
        recs = engine.generate_recommendations("run-rec")
        scopes = [r.target_scope for r in recs]
        self.assertNotIn("overall", scopes)

    def test_actionable_for_strong_signal(self):
        """Entry with 60 samples + stability + consistency → actionable."""
        from src.backtest.recommendations.recommendation_engine import RecommendationEngine
        from src.backtest.repositories.summary_repo import SummaryRepository
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.recommendation_repo import RecommendationRepository
        engine = RecommendationEngine(
            SummaryRepository(self.db),
            EvaluationRepository(self.db),
            RecommendationRepository(self.db),
        )
        recs = engine.generate_recommendations("run-rec")
        entry_rec = next((r for r in recs if r.target_key == "entry"), None)
        self.assertIsNotNone(entry_rec)
        self.assertEqual(entry_rec.recommendation_level, "actionable")
        self.assertEqual(entry_rec.recommendation_type, "weight_increase")

    def test_hypothesis_for_medium_sample(self):
        """mean_reversion with 25 samples + stability → hypothesis."""
        from src.backtest.recommendations.recommendation_engine import RecommendationEngine
        from src.backtest.repositories.summary_repo import SummaryRepository
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.recommendation_repo import RecommendationRepository
        engine = RecommendationEngine(
            SummaryRepository(self.db),
            EvaluationRepository(self.db),
            RecommendationRepository(self.db),
        )
        recs = engine.generate_recommendations("run-rec")
        mr_rec = next((r for r in recs if r.target_key == "mean_reversion"), None)
        self.assertIsNotNone(mr_rec)
        self.assertEqual(mr_rec.recommendation_level, "hypothesis")
        self.assertEqual(mr_rec.recommendation_type, "weight_decrease")

    def test_observation_for_small_sample(self):
        """event with 8 samples → observation only."""
        from src.backtest.recommendations.recommendation_engine import RecommendationEngine
        from src.backtest.repositories.summary_repo import SummaryRepository
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.recommendation_repo import RecommendationRepository
        engine = RecommendationEngine(
            SummaryRepository(self.db),
            EvaluationRepository(self.db),
            RecommendationRepository(self.db),
        )
        recs = engine.generate_recommendations("run-rec")
        ev_rec = next((r for r in recs if r.target_key == "event"), None)
        self.assertIsNotNone(ev_rec)
        self.assertEqual(ev_rec.recommendation_level, "observation")

    def test_skips_below_observation_min(self):
        """ULTRA with 3 samples → no recommendation."""
        from src.backtest.recommendations.recommendation_engine import RecommendationEngine
        from src.backtest.repositories.summary_repo import SummaryRepository
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.recommendation_repo import RecommendationRepository
        engine = RecommendationEngine(
            SummaryRepository(self.db),
            EvaluationRepository(self.db),
            RecommendationRepository(self.db),
        )
        recs = engine.generate_recommendations("run-rec")
        ultra_rec = next((r for r in recs if r.target_key == "ULTRA"), None)
        self.assertIsNone(ultra_rec)

    def test_execution_review_for_inconsistent(self):
        """volatile: win_rate>50 but return<0 → execution_review."""
        from src.backtest.recommendations.recommendation_engine import RecommendationEngine
        from src.backtest.repositories.summary_repo import SummaryRepository
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.recommendation_repo import RecommendationRepository
        engine = RecommendationEngine(
            SummaryRepository(self.db),
            EvaluationRepository(self.db),
            RecommendationRepository(self.db),
        )
        recs = engine.generate_recommendations("run-rec")
        vol_rec = next((r for r in recs if r.target_key == "volatile"), None)
        self.assertIsNotNone(vol_rec)
        self.assertEqual(vol_rec.recommendation_type, "execution_review")

    def test_evidence_json_populated(self):
        """Each recommendation should have evidence_json with audit trail."""
        from src.backtest.recommendations.recommendation_engine import RecommendationEngine
        from src.backtest.repositories.summary_repo import SummaryRepository
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.recommendation_repo import RecommendationRepository
        engine = RecommendationEngine(
            SummaryRepository(self.db),
            EvaluationRepository(self.db),
            RecommendationRepository(self.db),
        )
        recs = engine.generate_recommendations("run-rec")
        for rec in recs:
            self.assertIsNotNone(rec.evidence_json)
            evidence = json.loads(rec.evidence_json)
            self.assertIn("source_summary", evidence)
            self.assertIn("threshold_check", evidence)

    def test_recommendations_persisted(self):
        """Recommendations should be saved to DB."""
        from src.backtest.recommendations.recommendation_engine import RecommendationEngine
        from src.backtest.repositories.summary_repo import SummaryRepository
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.recommendation_repo import RecommendationRepository
        rec_repo = RecommendationRepository(self.db)
        engine = RecommendationEngine(
            SummaryRepository(self.db),
            EvaluationRepository(self.db),
            rec_repo,
        )
        engine.generate_recommendations("run-rec")
        persisted = rec_repo.get_by_run("run-rec")
        self.assertGreater(len(persisted), 0)

    def test_confidence_score_range(self):
        """All confidence scores should be between 0 and 1."""
        from src.backtest.recommendations.recommendation_engine import RecommendationEngine
        from src.backtest.repositories.summary_repo import SummaryRepository
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.recommendation_repo import RecommendationRepository
        engine = RecommendationEngine(
            SummaryRepository(self.db),
            EvaluationRepository(self.db),
            RecommendationRepository(self.db),
        )
        recs = engine.generate_recommendations("run-rec")
        for rec in recs:
            self.assertGreaterEqual(rec.confidence, 0.0)
            self.assertLessEqual(rec.confidence, 1.0)

    # ── F1/F2 regression guards ────────────────────────────────────────────

    def test_evidence_includes_sample_quality_and_inference_source(self):
        """F2: every recommendation must carry sample_quality + inference_source
        in evidence_json so reviewers can audit which family/subset drove the
        suggestion and how heavily-suppressed the source group was.
        """
        from src.backtest.recommendations.recommendation_engine import RecommendationEngine
        from src.backtest.repositories.summary_repo import SummaryRepository
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.recommendation_repo import RecommendationRepository

        engine = RecommendationEngine(
            SummaryRepository(self.db),
            EvaluationRepository(self.db),
            RecommendationRepository(self.db),
        )
        recs = engine.generate_recommendations("run-rec")
        self.assertGreater(len(recs), 0)
        for rec in recs:
            evidence = json.loads(rec.evidence_json)
            self.assertIn("sample_quality", evidence)
            self.assertIn("inference_source", evidence)
            quality = evidence["sample_quality"]
            self.assertIn("aggregatable_sample_count", quality)
            self.assertIn("aggregatable_ratio", quality)
            self.assertIn("suppressed_reasons", quality)

            inference = evidence["inference_source"]
            self.assertIn("family_scope", inference)
            self.assertIn(inference["family_scope"], {"entry", "observation", "mixed"})

    def test_signal_family_recommendation_uses_family_key_as_scope(self):
        """F1: ``signal_family`` rows are themselves single-family — the
        recommendation must record family_scope == group_key (e.g. "entry")
        rather than "mixed", so downstream readers know it is family-pure.
        """
        from src.backtest.recommendations.recommendation_engine import RecommendationEngine
        from src.backtest.repositories.summary_repo import SummaryRepository
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.recommendation_repo import RecommendationRepository

        engine = RecommendationEngine(
            SummaryRepository(self.db),
            EvaluationRepository(self.db),
            RecommendationRepository(self.db),
        )
        recs = engine.generate_recommendations("run-rec")
        entry_rec = next((r for r in recs if r.target_key == "entry"), None)
        self.assertIsNotNone(entry_rec)
        evidence = json.loads(entry_rec.evidence_json)
        self.assertEqual(evidence["inference_source"]["family_scope"], "entry")
        snapshot = json.loads(entry_rec.metrics_before_json)
        self.assertEqual(snapshot["family_scope"], "entry")

    def test_recommendation_prefers_family_breakdown_over_mixed_metrics(self):
        """F1 (key fix): when a mixed-capable summary exposes a family_breakdown
        with entry-only numbers that disagree with the legacy mixed numbers,
        the engine MUST drive the recommendation off the entry-only numbers.

        Setup: persist a setup_type row whose mixed win_rate=70 looks
        "weight_increase"-worthy, but whose family_breakdown.entry shows
        win_rate=30 / avg_return=-2 — i.e. the entry sub-population is bad
        and the mixed number was inflated by observation samples. The engine
        must recommend ``weight_decrease`` (driven by entry), not
        ``weight_increase`` (driven by mixed).
        """
        from src.backtest.recommendations.recommendation_engine import RecommendationEngine
        from src.backtest.repositories.summary_repo import SummaryRepository
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.recommendation_repo import RecommendationRepository

        sr = SummaryRepository(self.db)
        sr.upsert_summary(
            "run-rec",
            "setup_type",
            "family_split_demo",
            sample_count=60,
            avg_return_pct=3.0,         # MIXED looks great
            win_rate_pct=70.0,          # MIXED looks great
            time_bucket_stability=0.08,
            extreme_sample_ratio=0.03,
            metrics_json=json.dumps({
                "sample_baseline": {
                    "raw_sample_count": 60,
                    "aggregatable_sample_count": 60,
                    "suppressed_reasons": {},
                },
                "family_breakdown": {
                    "entry": {
                        "win_rate_pct": 30.0,        # entry is actually BAD
                        "avg_return_pct": -2.0,
                        "median_return_pct": -1.5,
                        "profit_factor": 0.5,
                        "sample_count": 50,
                    },
                    "observation": {
                        "win_rate_pct": 90.0,        # observation skewed mixed up
                        "avg_return_pct": 8.0,
                        "median_return_pct": 7.0,
                        "profit_factor": 4.0,
                        "sample_count": 10,
                    },
                },
            }, ensure_ascii=False),
        )

        engine = RecommendationEngine(
            SummaryRepository(self.db),
            EvaluationRepository(self.db),
            RecommendationRepository(self.db),
        )
        recs = engine.generate_recommendations("run-rec")
        rec = next((r for r in recs if r.target_key == "family_split_demo"), None)
        self.assertIsNotNone(rec)
        # Entry-only metrics drove the decision → weight_decrease, NOT
        # weight_increase (which the mixed numbers would have produced).
        self.assertEqual(rec.recommendation_type, "weight_decrease")

        evidence = json.loads(rec.evidence_json)
        self.assertEqual(evidence["inference_source"]["family_scope"], "entry")
        inference_metrics = evidence["inference_source"]["inference_metrics"]
        self.assertEqual(inference_metrics["win_rate_pct"], 30.0)
        self.assertEqual(inference_metrics["avg_return_pct"], -2.0)

    def test_recommendation_falls_back_to_observation_when_only_observation_data(self):
        """F1: if family_breakdown only carries observation data (no entry),
        the engine must still use observation metrics rather than the mixed
        summary numbers, and stamp family_scope="observation" in evidence.
        """
        from src.backtest.recommendations.recommendation_engine import RecommendationEngine
        from src.backtest.repositories.summary_repo import SummaryRepository
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.recommendation_repo import RecommendationRepository

        sr = SummaryRepository(self.db)
        sr.upsert_summary(
            "run-rec",
            "market_regime",
            "obs_only_demo",
            sample_count=25,
            avg_return_pct=0.0,
            win_rate_pct=0.0,
            time_bucket_stability=0.10,
            extreme_sample_ratio=0.05,
            metrics_json=json.dumps({
                "sample_baseline": {
                    "raw_sample_count": 25,
                    "aggregatable_sample_count": 25,
                    "suppressed_reasons": {},
                },
                "family_breakdown": {
                    "observation": {
                        "win_rate_pct": 25.0,
                        "avg_return_pct": -3.0,
                        "median_return_pct": -2.5,
                        "profit_factor": 0.4,
                        "sample_count": 25,
                    },
                },
            }, ensure_ascii=False),
        )

        engine = RecommendationEngine(
            SummaryRepository(self.db),
            EvaluationRepository(self.db),
            RecommendationRepository(self.db),
        )
        recs = engine.generate_recommendations("run-rec")
        rec = next((r for r in recs if r.target_key == "obs_only_demo"), None)
        self.assertIsNotNone(rec)
        evidence = json.loads(rec.evidence_json)
        self.assertEqual(evidence["inference_source"]["family_scope"], "observation")

    # ── Patch 2: stability gate uses family-correct metrics ────────────────

    def test_stability_gate_uses_family_correct_stability(self):
        """Patch 2 (key fix): when the mixed summary stability is bad but the
        family_breakdown.entry stability is good, the gate must trust the
        family-correct numbers (because the recommendation is itself driven
        by entry-only metrics). Otherwise the gate validates against a
        contaminated stability surface and silently kills good entry signals.

        Setup: sample=25 entry-friendly setup_type with
          - mixed time_bucket_stability=0.30   (FAIL by 0.15 threshold)
          - mixed extreme_sample_ratio=0.20    (FAIL by 0.10 threshold)
          - family_breakdown.entry tbs=0.05    (PASS)
          - family_breakdown.entry esr=0.02    (PASS)
        Expect: hypothesis-level recommendation (sample>=20 + stability_passed),
        not observation-only.
        """
        from src.backtest.recommendations.recommendation_engine import RecommendationEngine
        from src.backtest.repositories.summary_repo import SummaryRepository
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.recommendation_repo import RecommendationRepository

        sr = SummaryRepository(self.db)
        sr.upsert_summary(
            "run-rec",
            "setup_type",
            "stability_split_demo",
            sample_count=25,
            avg_return_pct=4.5,
            win_rate_pct=65.0,
            time_bucket_stability=0.30,    # mixed FAILS
            extreme_sample_ratio=0.20,     # mixed FAILS
            metrics_json=json.dumps({
                "sample_baseline": {
                    "raw_sample_count": 25,
                    "aggregatable_sample_count": 25,
                    "suppressed_reasons": {},
                },
                "family_breakdown": {
                    "entry": {
                        "win_rate_pct": 65.0,
                        "avg_return_pct": 4.5,
                        "median_return_pct": 4.0,
                        "profit_factor": 2.5,
                        "sample_count": 25,
                        "time_bucket_stability": 0.05,   # entry PASSES
                        "extreme_sample_ratio": 0.02,    # entry PASSES
                    },
                },
            }, ensure_ascii=False),
        )

        engine = RecommendationEngine(
            SummaryRepository(self.db),
            EvaluationRepository(self.db),
            RecommendationRepository(self.db),
        )
        recs = engine.generate_recommendations("run-rec")
        rec = next((r for r in recs if r.target_key == "stability_split_demo"), None)
        self.assertIsNotNone(rec)
        # Key assertion: stability gate trusted family-correct data.
        # Without the fix, level would be "observation" because the
        # mixed tbs=0.30/esr=0.20 fail the gate.
        self.assertEqual(rec.recommendation_level, "hypothesis")
        evidence = json.loads(rec.evidence_json)
        self.assertEqual(evidence["inference_source"]["family_scope"], "entry")

    def test_stability_gate_falls_back_to_summary_when_no_family_breakdown(self):
        """Patch 2: with no family_breakdown the metrics dict is built from
        the summary's mixed columns, so stability still uses those mixed
        columns (no behavioural regression for legacy rows).
        """
        from src.backtest.recommendations.recommendation_engine import RecommendationEngine
        from src.backtest.repositories.summary_repo import SummaryRepository
        from src.backtest.repositories.evaluation_repo import EvaluationRepository
        from src.backtest.repositories.recommendation_repo import RecommendationRepository

        sr = SummaryRepository(self.db)
        sr.upsert_summary(
            "run-rec",
            "setup_type",
            "no_breakdown_demo",
            sample_count=25,
            avg_return_pct=4.5,
            win_rate_pct=65.0,
            time_bucket_stability=0.30,    # FAIL
            extreme_sample_ratio=0.20,     # FAIL
            # NOTE: no metrics_json → no family_breakdown
        )

        engine = RecommendationEngine(
            SummaryRepository(self.db),
            EvaluationRepository(self.db),
            RecommendationRepository(self.db),
        )
        recs = engine.generate_recommendations("run-rec")
        rec = next((r for r in recs if r.target_key == "no_breakdown_demo"), None)
        self.assertIsNotNone(rec)
        # stability fails → demoted to observation
        self.assertEqual(rec.recommendation_level, "observation")


@pytest.mark.unit
class TestCheckStabilityUnit(unittest.TestCase):
    """Patch 2 unit-level coverage for ``_check_stability`` accepting a dict."""

    def test_passes_when_both_stability_axes_are_within_thresholds(self):
        from src.backtest.recommendations.recommendation_engine import _check_stability

        self.assertTrue(_check_stability({
            "time_bucket_stability": 0.05,
            "extreme_sample_ratio": 0.02,
        }))

    def test_fails_when_time_bucket_stability_exceeds_threshold(self):
        from src.backtest.recommendations.recommendation_engine import _check_stability

        self.assertFalse(_check_stability({
            "time_bucket_stability": 0.30,
            "extreme_sample_ratio": 0.02,
        }))

    def test_fails_when_extreme_sample_ratio_exceeds_threshold(self):
        from src.backtest.recommendations.recommendation_engine import _check_stability

        self.assertFalse(_check_stability({
            "time_bucket_stability": 0.05,
            "extreme_sample_ratio": 0.20,
        }))

    def test_passes_when_both_metrics_are_missing(self):
        """Missing stability metrics are treated as "no objection" so a small
        family slice doesn't get penalised for not having computable bucket
        statistics. The wider gate stack still relies on sample threshold
        and consistency to reject under-sourced recommendations.
        """
        from src.backtest.recommendations.recommendation_engine import _check_stability

        self.assertTrue(_check_stability({}))


@pytest.mark.unit
class TestResolveGradeMetricsSource(unittest.TestCase):
    """Patch 1: SystemGrader is fed family-correct metrics, not mixed ones."""

    @staticmethod
    def _make_summary(metrics_payload, win_rate_pct=70.0, profit_factor=3.0,
                      time_bucket_stability=0.05):
        from src.backtest.models.backtest_models import FiveLayerBacktestGroupSummary

        return FiveLayerBacktestGroupSummary(
            backtest_run_id="run-grade",
            group_type="overall",
            group_key="all",
            sample_count=100,
            win_rate_pct=win_rate_pct,
            profit_factor=profit_factor,
            time_bucket_stability=time_bucket_stability,
            metrics_json=json.dumps(metrics_payload, ensure_ascii=False) if metrics_payload else None,
        )

    def test_prefers_family_breakdown_entry_when_available(self):
        """Key fix: even if the mixed summary win_rate/profit_factor look
        great, the grader must read family_breakdown.entry so observation's
        always-non-negative ``risk_avoided_pct`` doesn't inflate the grade.
        """
        from src.backtest.services.backtest_service import _resolve_grade_metrics_source

        summary = self._make_summary(
            metrics_payload={
                "family_breakdown": {
                    "entry": {
                        "win_rate_pct": 35.0,
                        "profit_factor": 0.6,
                        "time_bucket_stability": 0.20,
                    },
                    "observation": {
                        "win_rate_pct": 90.0,
                        "profit_factor": 4.0,
                        "time_bucket_stability": 0.04,
                    },
                },
            },
            win_rate_pct=70.0,        # mixed contamination — should NOT win
            profit_factor=3.0,
        )
        wr, pf, tbs, source = _resolve_grade_metrics_source(summary)
        self.assertEqual(source, "entry")
        self.assertEqual(wr, 35.0)
        self.assertEqual(pf, 0.6)
        self.assertEqual(tbs, 0.20)

    def test_falls_back_to_observation_when_only_observation_present(self):
        from src.backtest.services.backtest_service import _resolve_grade_metrics_source

        summary = self._make_summary(metrics_payload={
            "family_breakdown": {
                "observation": {
                    "win_rate_pct": 25.0,
                    "profit_factor": 0.4,
                    "time_bucket_stability": 0.18,
                },
            },
        })
        wr, pf, tbs, source = _resolve_grade_metrics_source(summary)
        self.assertEqual(source, "observation")
        self.assertEqual(wr, 25.0)
        self.assertEqual(pf, 0.4)
        self.assertEqual(tbs, 0.18)

    def test_falls_back_to_mixed_when_family_breakdown_missing(self):
        from src.backtest.services.backtest_service import _resolve_grade_metrics_source

        summary = self._make_summary(metrics_payload={
            "sample_baseline": {"raw_sample_count": 100},
            # No family_breakdown
        })
        wr, pf, tbs, source = _resolve_grade_metrics_source(summary)
        self.assertEqual(source, "mixed")
        self.assertEqual(wr, 70.0)
        self.assertEqual(pf, 3.0)
        self.assertEqual(tbs, 0.05)

    def test_falls_back_to_mixed_when_metrics_json_is_corrupt(self):
        from src.backtest.services.backtest_service import _resolve_grade_metrics_source
        from src.backtest.models.backtest_models import FiveLayerBacktestGroupSummary

        summary = FiveLayerBacktestGroupSummary(
            backtest_run_id="run-grade",
            group_type="overall",
            group_key="all",
            sample_count=50,
            win_rate_pct=55.0,
            profit_factor=1.5,
            time_bucket_stability=0.10,
            metrics_json="{not valid json",  # corrupt
        )
        wr, pf, tbs, source = _resolve_grade_metrics_source(summary)
        self.assertEqual(source, "mixed")
        self.assertEqual(wr, 55.0)
        self.assertEqual(pf, 1.5)
        self.assertEqual(tbs, 0.10)

    def test_skips_family_breakdown_with_null_win_rate(self):
        """Don't promote a family_breakdown entry that has only sample_count
        but no usable win_rate (happens when no aggregatable returns were
        recorded for that family) — fall through to observation or mixed.
        """
        from src.backtest.services.backtest_service import _resolve_grade_metrics_source

        summary = self._make_summary(metrics_payload={
            "family_breakdown": {
                "entry": {
                    "win_rate_pct": None,
                    "profit_factor": None,
                    "time_bucket_stability": None,
                    "sample_count": 5,
                },
                "observation": {
                    "win_rate_pct": 80.0,
                    "profit_factor": 3.5,
                    "time_bucket_stability": 0.05,
                },
            },
        })
        wr, pf, tbs, source = _resolve_grade_metrics_source(summary)
        self.assertEqual(source, "observation")
        self.assertEqual(wr, 80.0)
        self.assertEqual(pf, 3.5)


@pytest.mark.unit
class TestSystemGrader(unittest.TestCase):
    """F3: SystemGrader sample-quality downgrade and reasons."""

    def test_grade_returns_na_below_min_sample_count(self):
        from src.backtest.aggregators.system_grader import SystemGrader

        result = SystemGrader.grade_with_reasons(
            win_rate_pct=70.0,
            profit_factor=2.5,
            time_bucket_stability=0.05,
            sample_count=8,
        )
        self.assertEqual(result.grade, "N/A")
        self.assertEqual(result.raw_grade, "N/A")
        self.assertFalse(result.downgraded)

    def test_grade_records_all_axis_reasons(self):
        from src.backtest.aggregators.system_grader import SystemGrader

        result = SystemGrader.grade_with_reasons(
            win_rate_pct=62.0,
            profit_factor=2.2,
            time_bucket_stability=0.06,
            sample_count=80,
        )
        # 40 + 40 + 20 = 100 → A+
        self.assertEqual(result.grade, "A+")
        self.assertEqual(result.raw_grade, "A+")
        # Each axis must contribute a reason line
        joined = " | ".join(result.reasons)
        self.assertIn("win_rate_pct", joined)
        self.assertIn("profit_factor", joined)
        self.assertIn("time_bucket_stability", joined)

    def test_grade_downgrades_when_aggregatable_ratio_too_low(self):
        """F3 (key fix): an A-quality metric set computed on heavily-suppressed
        samples must be downgraded one step so reviewers can immediately spot
        that the headline grade is not trustworthy."""
        from src.backtest.aggregators.system_grader import SystemGrader

        # Same metric inputs as the A+ test, but with raw_sample_count = 200
        # (so aggregatable_ratio = 80/200 = 0.4, well below the 0.6 threshold)
        result = SystemGrader.grade_with_reasons(
            win_rate_pct=62.0,
            profit_factor=2.2,
            time_bucket_stability=0.06,
            sample_count=80,
            raw_sample_count=200,
        )
        self.assertEqual(result.raw_grade, "A+")
        self.assertEqual(result.grade, "A")  # one step lower
        self.assertTrue(result.downgraded)
        self.assertEqual(result.aggregatable_ratio, 0.4)
        # The downgrade reason line must be appended for traceability
        self.assertTrue(
            any("downgrade" in r.lower() for r in result.reasons),
            f"Expected a downgrade reason in {result.reasons}",
        )

    def test_grade_does_not_downgrade_when_ratio_above_threshold(self):
        from src.backtest.aggregators.system_grader import SystemGrader

        result = SystemGrader.grade_with_reasons(
            win_rate_pct=62.0,
            profit_factor=2.2,
            time_bucket_stability=0.06,
            sample_count=80,
            raw_sample_count=100,  # ratio 0.8 ≥ 0.6
        )
        self.assertFalse(result.downgraded)
        self.assertEqual(result.grade, result.raw_grade)
        self.assertEqual(result.grade, "A+")

    def test_grade_legacy_str_signature_still_works(self):
        """Backwards compatibility: SystemGrader.grade(...) -> str must keep
        returning the letter grade for any caller still on the old API.
        """
        from src.backtest.aggregators.system_grader import SystemGrader

        grade = SystemGrader.grade(
            win_rate_pct=62.0,
            profit_factor=2.2,
            time_bucket_stability=0.06,
            sample_count=80,
        )
        self.assertEqual(grade, "A+")

    def test_grade_legacy_signature_accepts_raw_sample_count_kwarg(self):
        """The new raw_sample_count kwarg must also work via the legacy
        ``.grade()`` shim so the str-only callers can opt in incrementally."""
        from src.backtest.aggregators.system_grader import SystemGrader

        grade = SystemGrader.grade(
            win_rate_pct=62.0,
            profit_factor=2.2,
            time_bucket_stability=0.06,
            sample_count=80,
            raw_sample_count=200,
        )
        self.assertEqual(grade, "A")  # downgraded


if __name__ == "__main__":
    unittest.main()
