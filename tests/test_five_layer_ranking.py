# -*- coding: utf-8 -*-
"""TDD: Tests for RankingEffectivenessCalculator.

Validates that the screening system's tiered ranking (pool levels,
theme positions, maturity grades) actually predicts forward performance.
"""

import os
import tempfile
import unittest
from datetime import date

import pytest


@pytest.mark.unit
class TestRankingEffectivenessCalculator(unittest.TestCase):

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test_rank.db")
        os.environ["DATABASE_PATH"] = self._db_path
        from src.config import Config
        Config._instance = None
        from src.storage import DatabaseManager
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()
        self._seed_summaries()

    def tearDown(self):
        from src.storage import DatabaseManager
        from src.config import Config
        DatabaseManager.reset_instance()
        Config._instance = None
        os.environ.pop("DATABASE_PATH", None)
        self._temp_dir.cleanup()

    def _seed_summaries(self):
        """Seed group summaries simulating tiered performance.

        Pool levels: leader_pool (avg 5%) > focus_list (avg 2%) > watchlist (avg -1%)
        Theme positions: main_theme (avg 4%) > non_theme (avg 0%)
        Maturity: HIGH (avg 6%) > LOW (avg 1%)
        """
        from src.backtest.repositories.run_repo import RunRepository
        from src.backtest.repositories.summary_repo import SummaryRepository

        RunRepository(self.db).create_run(
            backtest_run_id="run-rank",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
            trade_date_from=date(2024, 1, 1),
            trade_date_to=date(2024, 3, 31),
            market="cn",
        )

        sr = SummaryRepository(self.db)

        # candidate_pool_level tiers
        sr.upsert_summary("run-rank", "candidate_pool_level", "leader_pool",
                          sample_count=20, avg_return_pct=5.0, win_rate_pct=65.0)
        sr.upsert_summary("run-rank", "candidate_pool_level", "focus_list",
                          sample_count=30, avg_return_pct=2.0, win_rate_pct=55.0)
        sr.upsert_summary("run-rank", "candidate_pool_level", "watchlist",
                          sample_count=40, avg_return_pct=-1.0, win_rate_pct=35.0)

        # theme_position tiers
        sr.upsert_summary("run-rank", "theme_position", "main_theme",
                          sample_count=25, avg_return_pct=4.0, win_rate_pct=60.0)
        sr.upsert_summary("run-rank", "theme_position", "non_theme",
                          sample_count=15, avg_return_pct=0.0, win_rate_pct=45.0)

        # entry_maturity tiers
        sr.upsert_summary("run-rank", "entry_maturity", "HIGH",
                          sample_count=10, avg_return_pct=6.0, win_rate_pct=70.0)
        sr.upsert_summary("run-rank", "entry_maturity", "LOW",
                          sample_count=12, avg_return_pct=1.0, win_rate_pct=48.0)

    def test_compute_returns_report(self):
        from src.backtest.aggregators.ranking_effectiveness import RankingEffectivenessCalculator
        from src.backtest.repositories.summary_repo import SummaryRepository
        summaries = SummaryRepository(self.db).get_by_run("run-rank")
        report = RankingEffectivenessCalculator.compute(summaries)
        self.assertIsNotNone(report)
        self.assertGreater(len(report.comparisons), 0)

    def test_pool_level_leader_outperforms_watchlist(self):
        from src.backtest.aggregators.ranking_effectiveness import RankingEffectivenessCalculator
        from src.backtest.repositories.summary_repo import SummaryRepository
        summaries = SummaryRepository(self.db).get_by_run("run-rank")
        report = RankingEffectivenessCalculator.compute(summaries)
        pool_comps = [c for c in report.comparisons
                      if c.dimension == "candidate_pool_level"
                      and c.tier_high == "leader_pool" and c.tier_low == "watchlist"]
        self.assertEqual(len(pool_comps), 1)
        self.assertTrue(pool_comps[0].is_effective)
        self.assertAlmostEqual(pool_comps[0].excess_return_pct, 6.0)

    def test_theme_position_main_outperforms_non(self):
        from src.backtest.aggregators.ranking_effectiveness import RankingEffectivenessCalculator
        from src.backtest.repositories.summary_repo import SummaryRepository
        summaries = SummaryRepository(self.db).get_by_run("run-rank")
        report = RankingEffectivenessCalculator.compute(summaries)
        theme_comps = [c for c in report.comparisons
                       if c.dimension == "theme_position"
                       and c.tier_high == "main_theme" and c.tier_low == "non_theme"]
        self.assertEqual(len(theme_comps), 1)
        self.assertTrue(theme_comps[0].is_effective)

    def test_maturity_high_outperforms_low(self):
        from src.backtest.aggregators.ranking_effectiveness import RankingEffectivenessCalculator
        from src.backtest.repositories.summary_repo import SummaryRepository
        summaries = SummaryRepository(self.db).get_by_run("run-rank")
        report = RankingEffectivenessCalculator.compute(summaries)
        mat_comps = [c for c in report.comparisons
                     if c.dimension == "entry_maturity"
                     and c.tier_high == "HIGH" and c.tier_low == "LOW"]
        self.assertEqual(len(mat_comps), 1)
        self.assertTrue(mat_comps[0].is_effective)
        self.assertAlmostEqual(mat_comps[0].excess_return_pct, 5.0)

    def test_overall_effectiveness_ratio(self):
        """All tiers should be effective → ratio = 1.0."""
        from src.backtest.aggregators.ranking_effectiveness import RankingEffectivenessCalculator
        from src.backtest.repositories.summary_repo import SummaryRepository
        summaries = SummaryRepository(self.db).get_by_run("run-rank")
        report = RankingEffectivenessCalculator.compute(summaries)
        self.assertAlmostEqual(report.overall_effectiveness_ratio, 1.0)

    def test_excess_return_leader_vs_watchlist(self):
        """excess_return_pct = leader(5%) - watchlist(-1%) = 6%."""
        from src.backtest.aggregators.ranking_effectiveness import RankingEffectivenessCalculator
        from src.backtest.repositories.summary_repo import SummaryRepository
        summaries = SummaryRepository(self.db).get_by_run("run-rank")
        report = RankingEffectivenessCalculator.compute(summaries)
        self.assertAlmostEqual(report.excess_return_pct, 6.0)

    def test_ranking_consistency(self):
        """All comparisons effective → consistency = 1.0."""
        from src.backtest.aggregators.ranking_effectiveness import RankingEffectivenessCalculator
        from src.backtest.repositories.summary_repo import SummaryRepository
        summaries = SummaryRepository(self.db).get_by_run("run-rank")
        report = RankingEffectivenessCalculator.compute(summaries)
        self.assertAlmostEqual(report.ranking_consistency, 1.0)

    def test_leader_pool_win_share(self):
        """leader_pool wins / total wins across pool levels."""
        from src.backtest.aggregators.ranking_effectiveness import RankingEffectivenessCalculator
        from src.backtest.repositories.summary_repo import SummaryRepository
        summaries = SummaryRepository(self.db).get_by_run("run-rank")
        report = RankingEffectivenessCalculator.compute(summaries)
        # leader: 20*0.65=13 wins, focus: 30*0.55=16.5, watchlist: 40*0.35=14
        # share = 13 / (13+16.5+14) = 13/43.5 ≈ 0.2989
        self.assertIsNotNone(report.leader_pool_win_share)
        self.assertAlmostEqual(report.leader_pool_win_share, 0.2989, places=3)

    def test_top_k_hit_rate_alias_matches_canonical(self):
        """Legacy alias must always equal the canonical metric."""
        from src.backtest.aggregators.ranking_effectiveness import RankingEffectivenessCalculator
        from src.backtest.repositories.summary_repo import SummaryRepository
        summaries = SummaryRepository(self.db).get_by_run("run-rank")
        report = RankingEffectivenessCalculator.compute(summaries)
        # ``top_k_hit_rate`` is preserved as a deprecated alias for backward
        # compatibility. It must carry exactly the same value so dashboards
        # reading either name see consistent data through the deprecation
        # window.
        self.assertEqual(report.top_k_hit_rate, report.leader_pool_win_share)

    def test_default_family_scope_is_entry(self):
        """Without overrides, ranking should anchor on entry family (D1)."""
        from src.backtest.aggregators.ranking_effectiveness import RankingEffectivenessCalculator
        from src.backtest.repositories.summary_repo import SummaryRepository
        summaries = SummaryRepository(self.db).get_by_run("run-rank")
        report = RankingEffectivenessCalculator.compute(summaries)
        self.assertEqual(report.family_scope, "entry")

    def test_mixed_fallback_when_no_family_breakdown(self):
        """Summaries without metrics_json must transparently fall back to mixed.

        The legacy seed has no ``family_breakdown`` payload, so the family-
        correct pass should silently use the mixed columns. The metric_source
        on each comparison should reflect that.
        """
        from src.backtest.aggregators.ranking_effectiveness import RankingEffectivenessCalculator
        from src.backtest.repositories.summary_repo import SummaryRepository
        summaries = SummaryRepository(self.db).get_by_run("run-rank")
        report = RankingEffectivenessCalculator.compute(summaries)
        sources = {c.metric_source for c in report.comparisons}
        # All comparisons should be tagged mixed_fallback because no tier
        # has a family_breakdown payload populated.
        self.assertIn("mixed_fallback", sources)
        # And the family-correct metric should match the mixed-legacy alias
        # (because the fallback path reads the same columns).
        self.assertEqual(
            report.leader_pool_win_share,
            report.leader_pool_win_share_mixed,
        )
        self.assertEqual(
            report.excess_return_pct,
            report.excess_return_pct_mixed,
        )


@pytest.mark.unit
class TestRankingEffectivenessFamilyCorrect(unittest.TestCase):
    """D1: when family_breakdown is present, ranking must use it."""

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test_rank_family.db")
        os.environ["DATABASE_PATH"] = self._db_path
        from src.config import Config
        Config._instance = None
        from src.storage import DatabaseManager
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()
        self._seed_summaries_with_family_breakdown()

    def tearDown(self):
        from src.storage import DatabaseManager
        from src.config import Config
        DatabaseManager.reset_instance()
        Config._instance = None
        os.environ.pop("DATABASE_PATH", None)
        self._temp_dir.cleanup()

    def _seed_summaries_with_family_breakdown(self):
        """Seed pool tiers where mixed lies but entry tells the truth.

        Scenario: leader_pool's headline mixed win_rate looks dominant
        (75%), but that is because 80% of its samples are observation
        (risk_avoided_pct, almost always non-negative → win=1.0). Its
        actual entry-family win_rate is only 40%. Watchlist is the
        opposite: low mixed win_rate (35%) but a healthy 55% entry
        win_rate.

        A pre-D1 ranking would say leader_pool ≫ watchlist; a D1 family-
        correct ranking should reverse the verdict.
        """
        import json
        from src.backtest.repositories.run_repo import RunRepository
        from src.backtest.repositories.summary_repo import SummaryRepository

        RunRepository(self.db).create_run(
            backtest_run_id="run-family",
            evaluation_mode="historical_snapshot",
            execution_model="conservative",
            trade_date_from=date(2024, 1, 1),
            trade_date_to=date(2024, 3, 31),
            market="cn",
        )

        sr = SummaryRepository(self.db)

        leader_metrics = json.dumps({
            "family_breakdown": {
                "entry": {
                    "sample_count": 10,
                    "aggregatable_sample_count": 10,
                    "avg_return_pct": -0.5,
                    "win_rate_pct": 40.0,
                },
                "observation": {
                    "sample_count": 40,
                    "aggregatable_sample_count": 40,
                    "avg_return_pct": 3.0,
                    "win_rate_pct": 90.0,
                },
            },
        })
        sr.upsert_summary("run-family", "candidate_pool_level", "leader_pool",
                          sample_count=50, avg_return_pct=2.3, win_rate_pct=75.0,
                          metrics_json=leader_metrics)

        watch_metrics = json.dumps({
            "family_breakdown": {
                "entry": {
                    "sample_count": 30,
                    "aggregatable_sample_count": 30,
                    "avg_return_pct": 1.5,
                    "win_rate_pct": 55.0,
                },
                "observation": {
                    "sample_count": 10,
                    "aggregatable_sample_count": 10,
                    "avg_return_pct": -1.0,
                    "win_rate_pct": 25.0,
                },
            },
        })
        sr.upsert_summary("run-family", "candidate_pool_level", "watchlist",
                          sample_count=40, avg_return_pct=0.9, win_rate_pct=35.0,
                          metrics_json=watch_metrics)

    def test_family_correct_excess_return_uses_entry_only(self):
        """excess_return must read entry's avg_return, not the mixed column."""
        from src.backtest.aggregators.ranking_effectiveness import RankingEffectivenessCalculator
        from src.backtest.repositories.summary_repo import SummaryRepository
        summaries = SummaryRepository(self.db).get_by_run("run-family")
        report = RankingEffectivenessCalculator.compute(summaries)
        # Entry-correct: leader entry avg = -0.5, watchlist entry avg = 1.5,
        # so entry-anchored excess = -2.0 (leader actually underperforms!).
        self.assertAlmostEqual(report.excess_return_pct, -2.0, places=2)
        # Mixed-legacy: 2.3 - 0.9 = 1.4, the contaminated number that
        # would have made leader_pool look great pre-D1.
        self.assertAlmostEqual(report.excess_return_pct_mixed, 1.4, places=2)
        # The two views must be detectably different so analysts can see
        # the family-mix bias on this run.
        self.assertNotAlmostEqual(
            report.excess_return_pct, report.excess_return_pct_mixed,
            places=2,
        )

    def test_family_correct_win_share_uses_entry_only(self):
        """leader_pool_win_share must read entry win_rate, not mixed."""
        from src.backtest.aggregators.ranking_effectiveness import RankingEffectivenessCalculator
        from src.backtest.repositories.summary_repo import SummaryRepository
        summaries = SummaryRepository(self.db).get_by_run("run-family")
        report = RankingEffectivenessCalculator.compute(summaries)
        # Entry: leader 10*0.40=4 wins, watch 30*0.55=16.5 wins; share = 4/20.5 ≈ 0.1951
        self.assertIsNotNone(report.leader_pool_win_share)
        self.assertAlmostEqual(report.leader_pool_win_share, 0.1951, places=3)
        # Mixed: leader 50*0.75=37.5, watch 40*0.35=14; share = 37.5/51.5 ≈ 0.7282
        self.assertIsNotNone(report.leader_pool_win_share_mixed)
        self.assertAlmostEqual(report.leader_pool_win_share_mixed, 0.7282, places=3)

    def test_comparison_metric_source_tag_is_family_entry(self):
        """When both tiers expose family_breakdown, source must be tagged."""
        from src.backtest.aggregators.ranking_effectiveness import RankingEffectivenessCalculator
        from src.backtest.repositories.summary_repo import SummaryRepository
        summaries = SummaryRepository(self.db).get_by_run("run-family")
        report = RankingEffectivenessCalculator.compute(summaries)
        pool_comps = [c for c in report.comparisons
                      if c.dimension == "candidate_pool_level"]
        self.assertEqual(len(pool_comps), 1)
        self.assertEqual(pool_comps[0].metric_source, "family_entry")

    def test_observation_scope_isolates_observation_family(self):
        """Asking for observation scope must return observation numbers."""
        from src.backtest.aggregators.ranking_effectiveness import RankingEffectivenessCalculator
        from src.backtest.repositories.summary_repo import SummaryRepository
        summaries = SummaryRepository(self.db).get_by_run("run-family")
        report = RankingEffectivenessCalculator.compute(
            summaries, family_scope="observation",
        )
        # Observation: leader 40*0.90=36 wins, watch 10*0.25=2.5 wins;
        # share = 36/38.5 ≈ 0.9351
        self.assertIsNotNone(report.leader_pool_win_share)
        self.assertAlmostEqual(report.leader_pool_win_share, 0.9351, places=3)
        # The mixed-legacy reference value remains the same regardless of
        # primary family_scope (it always reads the mixed columns).
        self.assertAlmostEqual(report.leader_pool_win_share_mixed, 0.7282, places=3)
        # And the comparison must be tagged with the observation source.
        pool_comps = [c for c in report.comparisons
                      if c.dimension == "candidate_pool_level"]
        self.assertEqual(pool_comps[0].metric_source, "family_observation")


if __name__ == "__main__":
    unittest.main()
