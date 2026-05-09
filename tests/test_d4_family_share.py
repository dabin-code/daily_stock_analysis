# -*- coding: utf-8 -*-
"""D4 tests: family-share evidence + recommendation confidence discount.

Covers:
  * ``compute_family_share`` helper output schema and edge cases.
  * ``EvidenceBuilder`` exposing ``family_share`` under ``sample_quality`` and
    ``dominant_family_match`` under ``inference_source``.
  * ``_family_share_confidence_adjustment`` and ``_compute_confidence``
    discount paths, including the hard penalty below 20 % share.
  * Persistence of ``family_share`` inside ``metrics_before_json`` so
    reviewers can audit the discount path from the recommendation row alone.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock

import pytest


# ── Helpers ─────────────────────────────────────────────────────────────────

def _summary_with_family(entry_count: int, observation_count: int):
    """Build a MagicMock summary whose metrics_json carries family_breakdown."""
    s = MagicMock()
    s.group_type = "setup_type"
    s.group_key = "trend_breakout"
    s.sample_count = entry_count + observation_count
    s.avg_return_pct = 1.5
    s.win_rate_pct = 55.0
    s.median_return_pct = 1.0
    s.profit_factor = 1.4
    s.time_bucket_stability = 0.05
    s.extreme_sample_ratio = 0.05
    s.p25_return_pct = -0.5
    s.p75_return_pct = 3.0

    payload = {
        "sample_baseline": {
            "raw_sample_count": entry_count + observation_count,
            "aggregatable_sample_count": entry_count + observation_count,
        },
        "family_breakdown": {
            "entry": {
                "sample_count": entry_count,
                "aggregatable_sample_count": entry_count,
                "avg_return_pct": 2.0,
                "win_rate_pct": 60.0,
            },
            "observation": {
                "sample_count": observation_count,
                "aggregatable_sample_count": observation_count,
                "avg_return_pct": 1.0,
                "win_rate_pct": 80.0,
            },
        },
    }
    s.metrics_json = json.dumps(payload)
    return s


def _summary_without_family():
    """Mock summary whose metrics_json carries no family_breakdown."""
    s = MagicMock()
    s.group_type = "setup_type"
    s.group_key = "trend_breakout"
    s.sample_count = 50
    # Explicit numeric attributes so EvidenceBuilder.build can JSON-serialize
    # the source_summary block without choking on MagicMock placeholders.
    s.avg_return_pct = 1.5
    s.win_rate_pct = 55.0
    s.median_return_pct = 1.0
    s.profit_factor = 1.4
    s.time_bucket_stability = 0.05
    s.extreme_sample_ratio = 0.05
    s.p25_return_pct = -0.5
    s.p75_return_pct = 3.0
    s.metrics_json = json.dumps({
        "sample_baseline": {
            "raw_sample_count": 50,
            "aggregatable_sample_count": 50,
        },
    })
    return s


# ── compute_family_share helper ────────────────────────────────────────────

@pytest.mark.unit
class TestComputeFamilyShare(unittest.TestCase):

    def test_balanced_split(self):
        from src.backtest.utils.summary_metrics import compute_family_share
        summary = _summary_with_family(entry_count=20, observation_count=20)
        share = compute_family_share(summary)
        self.assertTrue(share["available"])
        self.assertEqual(share["entry_sample_count"], 20)
        self.assertEqual(share["observation_sample_count"], 20)
        self.assertEqual(share["total_family_sample_count"], 40)
        self.assertAlmostEqual(share["entry_share"], 0.5)
        self.assertAlmostEqual(share["observation_share"], 0.5)
        # Tie goes to entry (the doc'd convention).
        self.assertEqual(share["dominant_family"], "entry")
        self.assertAlmostEqual(share["dominant_share"], 0.5)

    def test_observation_dominant(self):
        from src.backtest.utils.summary_metrics import compute_family_share
        summary = _summary_with_family(entry_count=4, observation_count=36)
        share = compute_family_share(summary)
        self.assertTrue(share["available"])
        self.assertAlmostEqual(share["entry_share"], 0.1)
        self.assertAlmostEqual(share["observation_share"], 0.9)
        self.assertEqual(share["dominant_family"], "observation")
        self.assertAlmostEqual(share["dominant_share"], 0.9)

    def test_entry_only(self):
        from src.backtest.utils.summary_metrics import compute_family_share
        summary = _summary_with_family(entry_count=30, observation_count=0)
        share = compute_family_share(summary)
        self.assertTrue(share["available"])
        self.assertAlmostEqual(share["entry_share"], 1.0)
        self.assertAlmostEqual(share["observation_share"], 0.0)
        self.assertEqual(share["dominant_family"], "entry")

    def test_no_family_breakdown_returns_unavailable(self):
        from src.backtest.utils.summary_metrics import compute_family_share
        summary = _summary_without_family()
        share = compute_family_share(summary)
        self.assertFalse(share["available"])
        self.assertEqual(share["entry_sample_count"], 0)
        self.assertEqual(share["observation_sample_count"], 0)
        self.assertIsNone(share["entry_share"])
        self.assertIsNone(share["dominant_family"])

    def test_zero_total_family_counts_marks_available(self):
        """Degenerate but legal: family_breakdown with zero counts."""
        from src.backtest.utils.summary_metrics import compute_family_share
        s = MagicMock()
        s.metrics_json = json.dumps({
            "family_breakdown": {
                "entry": {"sample_count": 0, "aggregatable_sample_count": 0},
                "observation": {"sample_count": 0, "aggregatable_sample_count": 0},
            },
        })
        share = compute_family_share(s)
        # Available because the block is present, but no shares to report.
        self.assertTrue(share["available"])
        self.assertEqual(share["total_family_sample_count"], 0)
        self.assertIsNone(share["entry_share"])
        self.assertIsNone(share["dominant_family"])

    def test_malformed_metrics_json_returns_unavailable(self):
        from src.backtest.utils.summary_metrics import compute_family_share
        s = MagicMock()
        s.metrics_json = "{not valid json"
        share = compute_family_share(s)
        self.assertFalse(share["available"])

    def test_aggregatable_count_preferred_over_raw(self):
        """When both fields exist, aggregatable should win."""
        from src.backtest.utils.summary_metrics import compute_family_share
        s = MagicMock()
        s.metrics_json = json.dumps({
            "family_breakdown": {
                "entry": {"sample_count": 100, "aggregatable_sample_count": 60},
                "observation": {"sample_count": 100, "aggregatable_sample_count": 40},
            },
        })
        share = compute_family_share(s)
        self.assertEqual(share["entry_sample_count"], 60)
        self.assertEqual(share["observation_sample_count"], 40)
        self.assertAlmostEqual(share["entry_share"], 0.6)


# ── evidence_builder integration ───────────────────────────────────────────

@pytest.mark.unit
class TestEvidenceBuilderFamilyShare(unittest.TestCase):

    def _build_evidence(
        self,
        summary,
        family_scope: str = "entry",
        sample_count: int = 30,
    ) -> dict:
        from src.backtest.aggregators.sample_threshold import SampleThresholdGate
        from src.backtest.recommendations.evidence_builder import EvidenceBuilder
        threshold = SampleThresholdGate.check(sample_count)
        evidence_str = EvidenceBuilder.build(
            group_summary=summary,
            evaluations_sample=[],
            threshold_result=threshold,
            family_scope=family_scope,
            inference_metrics={"win_rate_pct": 60.0, "avg_return_pct": 2.0},
        )
        return json.loads(evidence_str)

    def test_evidence_exposes_family_share(self):
        summary = _summary_with_family(entry_count=20, observation_count=20)
        evidence = self._build_evidence(summary)
        self.assertIn("family_share", evidence["sample_quality"])
        self.assertTrue(evidence["sample_quality"]["family_share"]["available"])
        self.assertEqual(
            evidence["sample_quality"]["family_share"]["entry_sample_count"],
            20,
        )

    def test_dominant_family_match_true_when_inference_matches(self):
        summary = _summary_with_family(entry_count=30, observation_count=10)
        evidence = self._build_evidence(summary, family_scope="entry")
        # entry is dominant (75 %) and inference is entry → match.
        self.assertTrue(evidence["inference_source"]["dominant_family_match"])

    def test_dominant_family_match_false_when_inference_minority(self):
        summary = _summary_with_family(entry_count=5, observation_count=35)
        evidence = self._build_evidence(summary, family_scope="entry")
        # entry is the minority (12.5 %) — inference doesn't match dominant.
        self.assertFalse(evidence["inference_source"]["dominant_family_match"])

    def test_dominant_family_match_false_when_no_breakdown(self):
        summary = _summary_without_family()
        evidence = self._build_evidence(summary, family_scope="entry")
        # No breakdown means we can't claim a match.
        self.assertFalse(evidence["inference_source"]["dominant_family_match"])
        self.assertFalse(evidence["sample_quality"]["family_share"]["available"])


# ── confidence discount ────────────────────────────────────────────────────

@pytest.mark.unit
class TestFamilyShareConfidenceAdjustment(unittest.TestCase):

    def test_dominant_family_gets_bonus(self):
        from src.backtest.recommendations.recommendation_engine import (
            FAMILY_SHARE_DOMINANT_BONUS,
            _family_share_confidence_adjustment,
        )
        share = {"available": True, "entry_share": 0.7, "observation_share": 0.3}
        delta = _family_share_confidence_adjustment("entry", share, "setup_type")
        self.assertAlmostEqual(delta, FAMILY_SHARE_DOMINANT_BONUS)

    def test_minority_inference_gets_strict_penalty(self):
        from src.backtest.recommendations.recommendation_engine import (
            FAMILY_SHARE_PENALTY_STRICT,
            _family_share_confidence_adjustment,
        )
        share = {"available": True, "entry_share": 0.30, "observation_share": 0.70}
        delta = _family_share_confidence_adjustment("entry", share, "setup_type")
        self.assertAlmostEqual(delta, -FAMILY_SHARE_PENALTY_STRICT)

    def test_hard_minority_gets_hard_penalty(self):
        from src.backtest.recommendations.recommendation_engine import (
            FAMILY_SHARE_PENALTY_HARD,
            _family_share_confidence_adjustment,
        )
        share = {"available": True, "entry_share": 0.10, "observation_share": 0.90}
        delta = _family_share_confidence_adjustment("entry", share, "setup_type")
        self.assertAlmostEqual(delta, -FAMILY_SHARE_PENALTY_HARD)

    def test_signal_family_skips_adjustment(self):
        """signal_family rows are single-family — no share adjustment."""
        from src.backtest.recommendations.recommendation_engine import (
            _family_share_confidence_adjustment,
        )
        share = {"available": True, "entry_share": 1.0, "observation_share": 0.0}
        delta = _family_share_confidence_adjustment("entry", share, "signal_family")
        self.assertEqual(delta, 0.0)

    def test_unavailable_share_skips_adjustment(self):
        from src.backtest.recommendations.recommendation_engine import (
            _family_share_confidence_adjustment,
        )
        share = {"available": False}
        delta = _family_share_confidence_adjustment("entry", share, "setup_type")
        self.assertEqual(delta, 0.0)

    def test_mixed_inference_gets_strict_penalty(self):
        """Mixed-family inference is always penalised because the underlying
        numbers are contaminated."""
        from src.backtest.recommendations.recommendation_engine import (
            FAMILY_SHARE_PENALTY_STRICT,
            _family_share_confidence_adjustment,
        )
        share = {"available": True, "entry_share": 0.5, "observation_share": 0.5}
        delta = _family_share_confidence_adjustment("mixed", share, "setup_type")
        self.assertAlmostEqual(delta, -FAMILY_SHARE_PENALTY_STRICT)


@pytest.mark.unit
class TestComputeConfidenceWithFamilyShare(unittest.TestCase):

    def _build_draft(
        self,
        family_scope: str,
        family_share: dict,
        target_scope: str = "setup_type",
        sample_count: int = 60,
        stability_passed: bool = True,
        consistency_passed: bool = True,
    ):
        from src.backtest.aggregators.sample_threshold import SampleThresholdGate
        from src.backtest.recommendations.recommendation_engine import (
            RecommendationDraft,
        )
        threshold = SampleThresholdGate.check(sample_count)
        return RecommendationDraft(
            group_summary=MagicMock(),
            recommendation_type="weight_increase",
            target_scope=target_scope,
            target_key="trend_breakout",
            current_rule="",
            suggested_change="",
            threshold_result=threshold,
            stability_passed=stability_passed,
            consistency_passed=consistency_passed,
            family_scope=family_scope,
            inference_metrics={},
            family_share=family_share,
        )

    def test_full_score_with_dominant_family(self):
        from src.backtest.recommendations.recommendation_engine import _compute_confidence
        # Sample (0.4) + stability (0.3) + consistency (0.3) + dominant bonus (0.05) = 1.05 → clamped to 1.0
        draft = self._build_draft(
            family_scope="entry",
            family_share={"available": True, "entry_share": 0.8, "observation_share": 0.2},
        )
        conf = _compute_confidence(draft)
        self.assertAlmostEqual(conf, 1.0)

    def test_minority_family_drops_score(self):
        from src.backtest.recommendations.recommendation_engine import (
            FAMILY_SHARE_PENALTY_STRICT,
            _compute_confidence,
        )
        draft = self._build_draft(
            family_scope="entry",
            family_share={"available": True, "entry_share": 0.3, "observation_share": 0.7},
        )
        conf = _compute_confidence(draft)
        # 0.4 + 0.3 + 0.3 - 0.10 = 0.90
        self.assertAlmostEqual(conf, 1.0 - FAMILY_SHARE_PENALTY_STRICT)

    def test_hard_minority_drops_score_significantly(self):
        from src.backtest.recommendations.recommendation_engine import (
            FAMILY_SHARE_PENALTY_HARD,
            _compute_confidence,
        )
        draft = self._build_draft(
            family_scope="entry",
            family_share={"available": True, "entry_share": 0.10, "observation_share": 0.90},
        )
        conf = _compute_confidence(draft)
        # 0.4 + 0.3 + 0.3 - 0.20 = 0.80
        self.assertAlmostEqual(conf, 1.0 - FAMILY_SHARE_PENALTY_HARD)

    def test_unavailable_share_keeps_legacy_score(self):
        """Without family_breakdown the engine must keep the legacy formula."""
        from src.backtest.recommendations.recommendation_engine import _compute_confidence
        draft = self._build_draft(
            family_scope="mixed",
            family_share={"available": False},
        )
        conf = _compute_confidence(draft)
        # 0.4 + 0.3 + 0.3 = 1.0 (no family adjustment applied)
        self.assertAlmostEqual(conf, 1.0)

    def test_score_floor_at_zero(self):
        from src.backtest.recommendations.recommendation_engine import _compute_confidence
        # Pathological: weak sample + no stability + no consistency + hard penalty
        draft = self._build_draft(
            family_scope="entry",
            family_share={"available": True, "entry_share": 0.05, "observation_share": 0.95},
            sample_count=8,                # observation only
            stability_passed=False,
            consistency_passed=False,
        )
        conf = _compute_confidence(draft)
        # 0.1 - 0.20 = -0.10 → clamped to 0.0
        self.assertGreaterEqual(conf, 0.0)


if __name__ == "__main__":
    unittest.main()
