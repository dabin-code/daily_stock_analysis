# -*- coding: utf-8 -*-
"""Phase 3 unit tests: aggregation, calibration, recommendation.

Covers:
  - SampleThresholdGate boundary tests
  - StabilityMetricsCalculator statistical correctness
  - GroupSummaryAggregator overall/dimension/combo aggregation
  - RankingEffectivenessCalculator tier comparisons
  - CalibrationOutputGenerator delta and decision logic
  - EvidenceBuilder evidence chain completeness
  - RecommendationEngine grading, gates, red line enforcement
"""

import json
import unittest
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest


# ── Mock helpers ────────────────────────────────────────────────────────────

def _make_eval(
    forward_return_5d: float = 3.0,
    outcome: str = "win",
    signal_family: str = "entry",
    snapshot_setup_type: str = "trend_breakout",
    snapshot_market_regime: str = "balanced",
    snapshot_theme_position: str = "main_theme",
    snapshot_candidate_pool_level: str = "leader_pool",
    snapshot_entry_maturity: str = "HIGH",
    snapshot_trade_stage: str = "probe_entry",
    mae: float = -2.0,
    mfe: float = 5.0,
    max_drawdown_from_peak: float = -3.0,
    trade_date: Optional[date] = None,
    risk_avoided_pct: Optional[float] = None,
    code: str = "000001",
    eval_id: int = 1,
):
    """Create a mock evaluation object with default five-layer fields."""
    mock = MagicMock()
    mock.id = eval_id
    mock.forward_return_5d = forward_return_5d
    mock.outcome = outcome
    mock.signal_family = signal_family
    mock.snapshot_setup_type = snapshot_setup_type
    mock.snapshot_market_regime = snapshot_market_regime
    mock.snapshot_theme_position = snapshot_theme_position
    mock.snapshot_candidate_pool_level = snapshot_candidate_pool_level
    mock.snapshot_entry_maturity = snapshot_entry_maturity
    mock.snapshot_trade_stage = snapshot_trade_stage
    mock.mae = mae
    mock.mfe = mfe
    mock.max_drawdown_from_peak = max_drawdown_from_peak
    mock.trade_date = trade_date or date(2026, 3, 10)
    mock.risk_avoided_pct = risk_avoided_pct
    mock.code = code
    return mock


def _make_summary(
    group_type: str = "setup_type",
    group_key: str = "trend_breakout",
    sample_count: int = 60,
    avg_return_pct: float = 3.5,
    median_return_pct: float = 2.8,
    win_rate_pct: float = 65.0,
    avg_mae: float = -2.0,
    avg_mfe: float = 5.0,
    avg_drawdown: float = -3.0,
    time_bucket_stability: float = 0.08,
    extreme_sample_ratio: float = 0.05,
    p25_return_pct: float = 0.5,
    p75_return_pct: float = 5.0,
):
    mock = MagicMock()
    mock.group_type = group_type
    mock.group_key = group_key
    mock.sample_count = sample_count
    mock.avg_return_pct = avg_return_pct
    mock.median_return_pct = median_return_pct
    mock.win_rate_pct = win_rate_pct
    mock.avg_mae = avg_mae
    mock.avg_mfe = avg_mfe
    mock.avg_drawdown = avg_drawdown
    mock.time_bucket_stability = time_bucket_stability
    mock.extreme_sample_ratio = extreme_sample_ratio
    mock.p25_return_pct = p25_return_pct
    mock.p75_return_pct = p75_return_pct
    mock.top_k_hit_rate = None
    mock.leader_pool_win_share = None
    mock.excess_return_pct = None
    mock.ranking_consistency = None
    mock.metrics_json = None
    return mock


# ═══════════════════════════════════════════════════════════════════════════
# 1. SampleThresholdGate
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSampleThresholdGate(unittest.TestCase):

    def test_below_observation_min(self):
        from src.backtest.aggregators.sample_threshold import SampleThresholdGate
        result = SampleThresholdGate.check(3)
        self.assertFalse(result.can_display)
        self.assertFalse(result.can_suggest)
        self.assertFalse(result.can_action)

    def test_at_observation_min(self):
        from src.backtest.aggregators.sample_threshold import SampleThresholdGate
        result = SampleThresholdGate.check(5)
        self.assertTrue(result.can_display)
        self.assertFalse(result.can_suggest)
        self.assertFalse(result.can_action)

    def test_at_suggestion_min(self):
        from src.backtest.aggregators.sample_threshold import SampleThresholdGate
        result = SampleThresholdGate.check(20)
        self.assertTrue(result.can_display)
        self.assertTrue(result.can_suggest)
        self.assertFalse(result.can_action)

    def test_at_actionable_min(self):
        from src.backtest.aggregators.sample_threshold import SampleThresholdGate
        result = SampleThresholdGate.check(50)
        self.assertTrue(result.can_display)
        self.assertTrue(result.can_suggest)
        self.assertTrue(result.can_action)
        self.assertTrue(result.can_compute_stability)

    def test_stability_threshold(self):
        from src.backtest.aggregators.sample_threshold import SampleThresholdGate
        result = SampleThresholdGate.check(25)
        self.assertFalse(result.can_compute_stability)
        result2 = SampleThresholdGate.check(30)
        self.assertTrue(result2.can_compute_stability)

    def test_result_is_frozen(self):
        from src.backtest.aggregators.sample_threshold import SampleThresholdGate
        result = SampleThresholdGate.check(10)
        with self.assertRaises(AttributeError):
            result.can_display = False


# ═══════════════════════════════════════════════════════════════════════════
# 2. StabilityMetricsCalculator
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestStabilityMetrics(unittest.TestCase):

    def test_empty_returns(self):
        from src.backtest.aggregators.stability_metrics import StabilityMetricsCalculator
        result = StabilityMetricsCalculator.compute([])
        self.assertIsNone(result.median)
        self.assertIsNone(result.p25)

    def test_median_calculation(self):
        from src.backtest.aggregators.stability_metrics import StabilityMetricsCalculator
        result = StabilityMetricsCalculator.compute([1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertAlmostEqual(result.median, 3.0, places=2)

    def test_percentiles(self):
        from src.backtest.aggregators.stability_metrics import StabilityMetricsCalculator
        result = StabilityMetricsCalculator.compute([1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertIsNotNone(result.p25)
        self.assertIsNotNone(result.p75)
        self.assertLess(result.p25, result.median)
        self.assertGreater(result.p75, result.median)

    def test_extreme_sample_ratio(self):
        from src.backtest.aggregators.stability_metrics import StabilityMetricsCalculator
        # Normal + 1 extreme outlier
        returns = [1.0, 2.0, 1.5, 2.5, 1.8, 2.2, 1.0, 2.0, 100.0, 1.5]
        result = StabilityMetricsCalculator.compute(returns)
        self.assertGreater(result.extreme_sample_ratio, 0.0)

    def test_time_bucket_stability_with_dates(self):
        from src.backtest.aggregators.stability_metrics import StabilityMetricsCalculator
        returns = [3.0, -1.0, 2.0, -2.0, 4.0, -3.0]
        dates = [
            date(2026, 3, 2), date(2026, 3, 3),  # week 10
            date(2026, 3, 9), date(2026, 3, 10),  # week 11
            date(2026, 3, 16), date(2026, 3, 17),  # week 12
        ]
        result = StabilityMetricsCalculator.compute(returns, dates)
        self.assertIsNotNone(result.time_bucket_stability)

    def test_time_bucket_stability_none_without_dates(self):
        from src.backtest.aggregators.stability_metrics import StabilityMetricsCalculator
        result = StabilityMetricsCalculator.compute([1.0, 2.0, 3.0])
        self.assertIsNone(result.time_bucket_stability)

    def test_result_is_frozen(self):
        from src.backtest.aggregators.stability_metrics import StabilityMetricsCalculator
        result = StabilityMetricsCalculator.compute([1.0, 2.0])
        with self.assertRaises(AttributeError):
            result.median = 999.0


# ═══════════════════════════════════════════════════════════════════════════
# 3. GroupSummaryAggregator (pure aggregation function)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestGroupSummaryAggregation(unittest.TestCase):

    def test_aggregate_group_basic(self):
        from src.backtest.aggregators.group_summary_aggregator import aggregate_group
        evals = [
            _make_eval(forward_return_5d=5.0, outcome="win", mae=-2.0, mfe=6.0),
            _make_eval(forward_return_5d=-3.0, outcome="loss", mae=-5.0, mfe=1.0),
            _make_eval(forward_return_5d=2.0, outcome="win", mae=-1.0, mfe=4.0),
        ]
        result = aggregate_group(evals, family_filter="entry")
        self.assertIsNotNone(result)
        self.assertEqual(result["sample_count"], 3)
        self.assertAlmostEqual(result["win_rate_pct"], 66.67, places=1)

    def test_aggregate_group_avg_return(self):
        from src.backtest.aggregators.group_summary_aggregator import aggregate_group
        evals = [
            _make_eval(forward_return_5d=10.0),
            _make_eval(forward_return_5d=20.0),
        ]
        result = aggregate_group(evals, family_filter="entry")
        self.assertAlmostEqual(result["avg_return_pct"], 15.0, places=2)

    def test_aggregate_group_empty(self):
        from src.backtest.aggregators.group_summary_aggregator import aggregate_group
        result = aggregate_group([])
        self.assertIsNone(result)

    def test_aggregate_includes_stability(self):
        from src.backtest.aggregators.group_summary_aggregator import aggregate_group
        evals = [
            _make_eval(forward_return_5d=v, trade_date=date(2026, 3, d))
            for d, v in [(2, 5.0), (3, -1.0), (9, 3.0), (10, -2.0), (16, 4.0)]
        ]
        result = aggregate_group(evals, family_filter="entry")
        self.assertIn("p25_return_pct", result)
        self.assertIn("extreme_sample_ratio", result)

    def test_observation_evals_use_risk_avoided(self):
        from src.backtest.aggregators.group_summary_aggregator import aggregate_group
        evals = [
            _make_eval(forward_return_5d=None, risk_avoided_pct=3.0, outcome="win"),
            _make_eval(forward_return_5d=None, risk_avoided_pct=-1.0, outcome="loss"),
        ]
        # 这条钉的是旧混族口径：族标记为 entry 但收益取自 risk_avoided_pct
        result = aggregate_group(evals, allow_mixed_families=True)
        self.assertIsNotNone(result)
        self.assertEqual(result["sample_count"], 2)


# ═══════════════════════════════════════════════════════════════════════════
# 4. RankingEffectivenessCalculator
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRankingEffectiveness(unittest.TestCase):

    def test_leader_vs_watchlist(self):
        from src.backtest.aggregators.ranking_effectiveness import RankingEffectivenessCalculator
        summaries = [
            _make_summary(group_type="candidate_pool_level", group_key="leader_pool",
                          avg_return_pct=5.0, win_rate_pct=70.0, sample_count=30),
            _make_summary(group_type="candidate_pool_level", group_key="watchlist",
                          avg_return_pct=1.0, win_rate_pct=45.0, sample_count=50),
        ]
        report = RankingEffectivenessCalculator.compute(summaries)
        self.assertEqual(len(report.comparisons), 1)
        self.assertTrue(report.comparisons[0].is_effective)
        self.assertAlmostEqual(report.comparisons[0].excess_return_pct, 4.0, places=2)

    def test_ranking_ineffective(self):
        from src.backtest.aggregators.ranking_effectiveness import RankingEffectivenessCalculator
        summaries = [
            _make_summary(group_type="candidate_pool_level", group_key="leader_pool",
                          avg_return_pct=1.0, win_rate_pct=40.0),
            _make_summary(group_type="candidate_pool_level", group_key="watchlist",
                          avg_return_pct=3.0, win_rate_pct=55.0),
        ]
        report = RankingEffectivenessCalculator.compute(summaries)
        self.assertFalse(report.comparisons[0].is_effective)

    def test_empty_summaries(self):
        from src.backtest.aggregators.ranking_effectiveness import RankingEffectivenessCalculator
        report = RankingEffectivenessCalculator.compute([])
        self.assertEqual(len(report.comparisons), 0)

    def test_excess_return_calculation(self):
        from src.backtest.aggregators.ranking_effectiveness import RankingEffectivenessCalculator
        summaries = [
            _make_summary(group_type="candidate_pool_level", group_key="leader_pool",
                          avg_return_pct=8.0, sample_count=20),
            _make_summary(group_type="candidate_pool_level", group_key="focus_list",
                          avg_return_pct=4.0, sample_count=30),
            _make_summary(group_type="candidate_pool_level", group_key="watchlist",
                          avg_return_pct=1.0, sample_count=50),
        ]
        report = RankingEffectivenessCalculator.compute(summaries)
        self.assertAlmostEqual(report.excess_return_pct, 7.0, places=2)


# ═══════════════════════════════════════════════════════════════════════════
# 5. CalibrationOutputGenerator
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestCalibrationOutputGenerator(unittest.TestCase):

    def test_accept_on_significant_improvement(self):
        from src.backtest.aggregators.calibration_output_generator import CalibrationOutputGenerator
        baseline = _make_summary(avg_return_pct=2.0, win_rate_pct=50.0,
                                 median_return_pct=1.5, avg_mae=-3.0, avg_mfe=4.0, avg_drawdown=-5.0)
        candidate = _make_summary(avg_return_pct=3.0, win_rate_pct=55.0,
                                  median_return_pct=2.5, avg_mae=-2.0, avg_mfe=5.0, avg_drawdown=-4.0)
        output = CalibrationOutputGenerator.generate(
            "run-1", "test_calibration", baseline, candidate,
            {"param": "old"}, {"param": "new"},
        )
        self.assertEqual(output.decision, "accept")
        self.assertGreater(output.confidence, 0.0)

    def test_reject_on_degradation(self):
        from src.backtest.aggregators.calibration_output_generator import CalibrationOutputGenerator
        baseline = _make_summary(avg_return_pct=5.0, win_rate_pct=60.0,
                                 median_return_pct=4.0, avg_mae=-2.0, avg_mfe=6.0, avg_drawdown=-3.0)
        candidate = _make_summary(avg_return_pct=2.0, win_rate_pct=40.0,
                                  median_return_pct=1.0, avg_mae=-5.0, avg_mfe=3.0, avg_drawdown=-6.0)
        output = CalibrationOutputGenerator.generate(
            "run-1", "test_calibration", baseline, candidate,
            {"param": "old"}, {"param": "new"},
        )
        self.assertEqual(output.decision, "reject")

    def test_inconclusive_on_mixed(self):
        from src.backtest.aggregators.calibration_output_generator import CalibrationOutputGenerator
        baseline = _make_summary(avg_return_pct=3.0, win_rate_pct=50.0,
                                 median_return_pct=2.5, avg_mae=-3.0, avg_mfe=4.0, avg_drawdown=-5.0)
        candidate = _make_summary(avg_return_pct=3.1, win_rate_pct=49.0,
                                  median_return_pct=2.6, avg_mae=-3.1, avg_mfe=3.9, avg_drawdown=-5.1)
        output = CalibrationOutputGenerator.generate(
            "run-1", "test_calibration", baseline, candidate,
            {"param": "old"}, {"param": "new"},
        )
        self.assertEqual(output.decision, "inconclusive")

    def test_delta_metrics_json_populated(self):
        from src.backtest.aggregators.calibration_output_generator import CalibrationOutputGenerator
        baseline = _make_summary(avg_return_pct=2.0, win_rate_pct=50.0,
                                 median_return_pct=1.5, avg_mae=-3.0, avg_mfe=4.0, avg_drawdown=-5.0)
        candidate = _make_summary(avg_return_pct=4.0, win_rate_pct=60.0,
                                  median_return_pct=3.0, avg_mae=-2.0, avg_mfe=5.0, avg_drawdown=-4.0)
        output = CalibrationOutputGenerator.generate(
            "run-1", "test_cal", baseline, candidate, {}, {},
        )
        deltas = json.loads(output.delta_metrics_json)
        self.assertIn("avg_return_pct", deltas)
        self.assertIn("win_rate_pct", deltas)


# ═══════════════════════════════════════════════════════════════════════════
# 6. EvidenceBuilder
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestEvidenceBuilder(unittest.TestCase):

    def test_evidence_contains_summary_reference(self):
        from src.backtest.aggregators.sample_threshold import SampleThresholdGate
        from src.backtest.recommendations.evidence_builder import EvidenceBuilder
        summary = _make_summary()
        threshold = SampleThresholdGate.check(60)
        evals = [_make_eval(eval_id=i, code=f"00000{i}") for i in range(3)]
        evidence_str = EvidenceBuilder.build(summary, evals, threshold)
        evidence = json.loads(evidence_str)
        self.assertIn("source_summary", evidence)
        self.assertEqual(evidence["source_summary"]["group_type"], "setup_type")

    def test_evidence_contains_threshold_check(self):
        from src.backtest.aggregators.sample_threshold import SampleThresholdGate
        from src.backtest.recommendations.evidence_builder import EvidenceBuilder
        summary = _make_summary()
        threshold = SampleThresholdGate.check(10)
        evidence_str = EvidenceBuilder.build(summary, [], threshold)
        evidence = json.loads(evidence_str)
        self.assertIn("threshold_check", evidence)
        self.assertFalse(evidence["threshold_check"]["can_action"])

    def test_evidence_limits_sample_ids(self):
        from src.backtest.aggregators.sample_threshold import SampleThresholdGate
        from src.backtest.recommendations.evidence_builder import EvidenceBuilder
        summary = _make_summary()
        threshold = SampleThresholdGate.check(60)
        evals = [_make_eval(eval_id=i) for i in range(20)]
        evidence_str = EvidenceBuilder.build(summary, evals, threshold)
        evidence = json.loads(evidence_str)
        self.assertLessEqual(len(evidence["sample_evaluation_ids"]), 10)


# ═══════════════════════════════════════════════════════════════════════════
# 7. RecommendationEngine (gates & red lines)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRecommendationEngine(unittest.TestCase):

    def test_small_sample_never_actionable(self):
        """RED LINE: small samples CANNOT produce actionable."""
        from src.backtest.recommendations.recommendation_engine import _determine_level
        from src.backtest.aggregators.sample_threshold import SampleThresholdGate
        threshold = SampleThresholdGate.check(15)  # < SUGGESTION_MIN
        level = _determine_level(threshold, stability_passed=True, consistency_passed=True)
        self.assertNotEqual(level, "actionable")

    def test_actionable_requires_all_gates(self):
        from src.backtest.recommendations.recommendation_engine import _determine_level
        from src.backtest.aggregators.sample_threshold import SampleThresholdGate
        threshold = SampleThresholdGate.check(60)
        # Missing stability
        level = _determine_level(threshold, stability_passed=False, consistency_passed=True)
        self.assertNotEqual(level, "actionable")
        # Missing consistency
        level = _determine_level(threshold, stability_passed=True, consistency_passed=False)
        self.assertNotEqual(level, "actionable")
        # All gates pass
        level = _determine_level(threshold, stability_passed=True, consistency_passed=True)
        self.assertEqual(level, "actionable")

    def test_hypothesis_requires_stability(self):
        from src.backtest.recommendations.recommendation_engine import _determine_level
        from src.backtest.aggregators.sample_threshold import SampleThresholdGate
        threshold = SampleThresholdGate.check(25)
        level = _determine_level(threshold, stability_passed=False, consistency_passed=True)
        self.assertEqual(level, "observation")

    def test_observation_level_for_display_only(self):
        from src.backtest.recommendations.recommendation_engine import _determine_level
        from src.backtest.aggregators.sample_threshold import SampleThresholdGate
        threshold = SampleThresholdGate.check(8)
        level = _determine_level(threshold, stability_passed=False, consistency_passed=False)
        self.assertEqual(level, "observation")

    def test_below_observation_returns_none(self):
        from src.backtest.recommendations.recommendation_engine import _determine_level
        from src.backtest.aggregators.sample_threshold import SampleThresholdGate
        threshold = SampleThresholdGate.check(3)
        level = _determine_level(threshold, stability_passed=True, consistency_passed=True)
        self.assertIsNone(level)

    # NOTE: After Patch 2, _check_stability / _check_consistency operate on
    # a metrics ``dict`` (so the gate reads from the same family-correct
    # surface that drove the recommendation inference). After Step 6 (F1),
    # _infer_recommendation also takes (summary, family_scope, metrics).
    # The helpers below adapt the legacy summary mocks to the new surface.

    @staticmethod
    def _summary_to_metrics(summary):
        return {
            "win_rate_pct": summary.win_rate_pct,
            "avg_return_pct": summary.avg_return_pct,
            "median_return_pct": summary.median_return_pct,
            "profit_factor": getattr(summary, "profit_factor", None),
            "sample_count": summary.sample_count,
            "time_bucket_stability": summary.time_bucket_stability,
            "extreme_sample_ratio": summary.extreme_sample_ratio,
        }

    def test_check_stability_passes(self):
        from src.backtest.recommendations.recommendation_engine import _check_stability
        summary = _make_summary(time_bucket_stability=0.08, extreme_sample_ratio=0.05)
        self.assertTrue(_check_stability(self._summary_to_metrics(summary)))

    def test_check_stability_fails_on_high_tbs(self):
        from src.backtest.recommendations.recommendation_engine import _check_stability
        summary = _make_summary(time_bucket_stability=0.20, extreme_sample_ratio=0.05)
        self.assertFalse(_check_stability(self._summary_to_metrics(summary)))

    def test_check_stability_fails_on_high_esr(self):
        from src.backtest.recommendations.recommendation_engine import _check_stability
        summary = _make_summary(time_bucket_stability=0.08, extreme_sample_ratio=0.15)
        self.assertFalse(_check_stability(self._summary_to_metrics(summary)))

    def test_check_consistency_positive(self):
        from src.backtest.recommendations.recommendation_engine import _check_consistency
        summary = _make_summary(avg_return_pct=3.0, win_rate_pct=55.0)
        self.assertTrue(_check_consistency(self._summary_to_metrics(summary)))

    def test_check_consistency_negative(self):
        from src.backtest.recommendations.recommendation_engine import _check_consistency
        summary = _make_summary(avg_return_pct=-2.0, win_rate_pct=35.0)
        self.assertTrue(_check_consistency(self._summary_to_metrics(summary)))

    def test_check_consistency_fails_on_mismatch(self):
        from src.backtest.recommendations.recommendation_engine import _check_consistency
        summary = _make_summary(avg_return_pct=3.0, win_rate_pct=40.0)
        self.assertFalse(_check_consistency(self._summary_to_metrics(summary)))

    def test_infer_recommendation_strong_signal(self):
        from src.backtest.recommendations.recommendation_engine import _infer_recommendation
        summary = _make_summary(win_rate_pct=65.0, avg_return_pct=4.0)
        rec_type, _, _ = _infer_recommendation(
            summary=summary,
            family_scope="entry",
            metrics=self._summary_to_metrics(summary),
        )
        self.assertEqual(rec_type, "weight_increase")

    def test_infer_recommendation_weak_signal(self):
        from src.backtest.recommendations.recommendation_engine import _infer_recommendation
        summary = _make_summary(win_rate_pct=35.0, avg_return_pct=-3.0)
        rec_type, _, _ = _infer_recommendation(
            summary=summary,
            family_scope="entry",
            metrics=self._summary_to_metrics(summary),
        )
        self.assertEqual(rec_type, "weight_decrease")

    def test_infer_recommendation_execution_review(self):
        from src.backtest.recommendations.recommendation_engine import _infer_recommendation
        summary = _make_summary(win_rate_pct=55.0, avg_return_pct=-1.0)
        rec_type, _, _ = _infer_recommendation(
            summary=summary,
            family_scope="entry",
            metrics=self._summary_to_metrics(summary),
        )
        self.assertEqual(rec_type, "execution_review")

    def test_infer_recommendation_no_action(self):
        from src.backtest.recommendations.recommendation_engine import _infer_recommendation
        summary = _make_summary(win_rate_pct=50.0, avg_return_pct=1.0)
        rec_type, _, _ = _infer_recommendation(
            summary=summary,
            family_scope="entry",
            metrics=self._summary_to_metrics(summary),
        )
        self.assertIsNone(rec_type)

    def test_engine_has_no_config_write_imports(self):
        """RED LINE: RecommendationEngine must not import config-modifying modules."""
        import src.backtest.recommendations.recommendation_engine as mod
        source = open(mod.__file__, "r", encoding="utf-8").read()
        self.assertNotIn("config_manager", source)
        self.assertNotIn("config_registry", source)
        self.assertNotIn("strategy_screening_engine", source)


if __name__ == "__main__":
    unittest.main()
