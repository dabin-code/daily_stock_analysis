# -*- coding: utf-8 -*-
"""Unit tests for HotThemeFactorEnricher."""

import unittest
from src.services.hot_theme_factor_enricher import HotThemeFactorEnricher
from src.services.theme_context_ingest_service import ExternalTheme, OpenClawThemeContext
from datetime import datetime


class HotThemeFactorEnricherTestCase(unittest.TestCase):
    """Test HotThemeFactorEnricher."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.enricher = HotThemeFactorEnricher()

    def test_enrich_snapshot_no_theme_context(self) -> None:
        """Test enriching snapshot with no theme context."""
        snapshot = {
            "code": "000001",
            "name": "机器人",
            "close": 10.5,
            "above_ma100": True,
            "base_leader_score": 18.0,
            "base_extreme_strength_score": 22.0,
        }

        enriched = self.enricher.enrich_snapshot(snapshot, theme_context=None)

        self.assertFalse(enriched["is_hot_theme_stock"])
        self.assertIsNone(enriched["primary_theme"])
        self.assertEqual(enriched["theme_match_score"], 0.0)
        self.assertEqual(enriched["theme_leader_score"], 0.0)
        self.assertEqual(enriched["leader_score"], 18.0)
        self.assertEqual(enriched["extreme_strength_score"], 22.0)
        self.assertEqual(enriched["leader_score_source"], "base")

    def test_enrich_snapshot_with_matching_theme(self) -> None:
        """Test enriching snapshot with matching theme."""
        snapshot = {
            "code": "000001",
            "name": "机器人",
            "close": 10.5,
            "above_ma100": True,
            "gap_breakaway": True,
            "pattern_123_low_trendline": False,
            "is_limit_up": True,
            "bottom_divergence_double_breakout": False,
            "volume_ratio": 1.5,
            "turnover_rate": 0.05,
            "circ_mv": 75_000_000_000,
            "breakout_ratio": 1.2,
            "ma100_breakout_days": 3,
        }

        theme = ExternalTheme(
            name="机器人",
            heat_score=90.0,
            confidence=0.85,
            catalyst_summary="政策催化",
            keywords=["机器人"],
            evidence=[],
        )

        theme_context = OpenClawThemeContext(
            source="openclaw",
            trade_date="2026-03-26",
            market="cn",
            themes=[theme],
            accepted_at=datetime.now().isoformat(),
        )

        enriched = self.enricher.enrich_snapshot(
            snapshot,
            theme_context=theme_context,
            boards=["机器人"],
        )

        self.assertTrue(enriched["is_hot_theme_stock"])
        self.assertEqual(enriched["primary_theme"], "机器人")
        self.assertGreater(enriched["theme_match_score"], 0.8)
        self.assertGreater(enriched["theme_leader_score"], 50)
        self.assertGreater(enriched["leader_score"], 50)
        self.assertGreater(enriched["extreme_strength_score"], 70)
        self.assertEqual(enriched["leader_score_source"], "theme")
        self.assertIn("MA100之上", enriched["extreme_strength_reasons"])
        self.assertIn("跳空突破", enriched["extreme_strength_reasons"])
        self.assertIn("涨停", enriched["extreme_strength_reasons"])

    def test_enrich_snapshot_no_matching_theme(self) -> None:
        """Test enriching snapshot with no matching theme."""
        snapshot = {
            "code": "000002",
            "name": "芯片",
            "close": 10.5,
            "above_ma100": True,
            "gap_breakaway": False,
            "pattern_123_low_trendline": False,
            "is_limit_up": False,
            "bottom_divergence_double_breakout": False,
            "volume_ratio": 1.0,
            "turnover_rate": 0.02,
            "circ_mv": 150_000_000_000,
            "breakout_ratio": 0.95,
            "ma100_breakout_days": 0,
        }

        theme = ExternalTheme(
            name="机器人",
            heat_score=90.0,
            confidence=0.85,
            catalyst_summary="政策催化",
            keywords=["机器人"],
            evidence=[],
        )

        theme_context = OpenClawThemeContext(
            source="openclaw",
            trade_date="2026-03-26",
            market="cn",
            themes=[theme],
            accepted_at=datetime.now().isoformat(),
        )

        enriched = self.enricher.enrich_snapshot(
            snapshot,
            theme_context=theme_context,
            boards=["芯片"],
        )

        self.assertFalse(enriched["is_hot_theme_stock"])
        self.assertIsNone(enriched["primary_theme"])
        self.assertEqual(enriched["leader_score"], 0)
        self.assertEqual(enriched["extreme_strength_score"], 0.0)
        self.assertEqual(enriched["theme_leader_score"], 0.0)
        self.assertEqual(enriched["leader_score_source"], "base")

    def test_enrich_snapshot_multiple_themes(self) -> None:
        """Test enriching snapshot with multiple themes (picks best match)."""
        snapshot = {
            "code": "000001",
            "name": "机器人",
            "close": 10.5,
            "above_ma100": True,
            "gap_breakaway": True,
            "pattern_123_low_trendline": False,
            "is_limit_up": True,
            "bottom_divergence_double_breakout": False,
            "volume_ratio": 1.5,
            "turnover_rate": 0.05,
            "circ_mv": 75_000_000_000,
            "breakout_ratio": 1.2,
            "ma100_breakout_days": 3,
        }

        themes = [
            ExternalTheme(
                name="芯片",
                heat_score=80.0,
                confidence=0.80,
                catalyst_summary="产业升级",
                keywords=["芯片"],
                evidence=[],
            ),
            ExternalTheme(
                name="机器人",
                heat_score=90.0,
                confidence=0.85,
                catalyst_summary="政策催化",
                keywords=["机器人"],
                evidence=[],
            ),
        ]

        theme_context = OpenClawThemeContext(
            source="openclaw",
            trade_date="2026-03-26",
            market="cn",
            themes=themes,
            accepted_at=datetime.now().isoformat(),
        )

        enriched = self.enricher.enrich_snapshot(
            snapshot,
            theme_context=theme_context,
            boards=["机器人"],
        )

        # Should pick "机器人" as best match
        self.assertTrue(enriched["is_hot_theme_stock"])
        self.assertEqual(enriched["primary_theme"], "机器人")
        self.assertEqual(enriched["theme_heat_score"], 90.0)

    def test_enrich_snapshot_preserves_original_fields(self) -> None:
        """Test enriching snapshot preserves original fields."""
        snapshot = {
            "code": "000001",
            "name": "机器人",
            "close": 10.5,
            "above_ma100": True,
            "gap_breakaway": True,
            "pattern_123_low_trendline": False,
            "is_limit_up": True,
            "bottom_divergence_double_breakout": False,
            "volume_ratio": 1.5,
            "turnover_rate": 0.05,
            "circ_mv": 75_000_000_000,
            "breakout_ratio": 1.2,
            "ma100_breakout_days": 3,
        }

        enriched = self.enricher.enrich_snapshot(snapshot, theme_context=None)

        # Original fields should be preserved
        self.assertEqual(enriched["code"], "000001")
        self.assertEqual(enriched["name"], "机器人")
        self.assertEqual(enriched["close"], 10.5)
        self.assertTrue(enriched["above_ma100"])

    def test_watching_low123_contributes_to_watchlist_strength(self) -> None:
        """watching low123 should add observation strength for hot themes."""
        snapshot = {
            "code": "000001",
            "name": "机器人",
            "close": 10.5,
            "above_ma100": True,
            "gap_breakaway": False,
            "pattern_123_low_trendline": False,
            "pattern_123_watchlist": True,
            "is_limit_up": False,
            "bottom_divergence_double_breakout": False,
            "volume_ratio": 1.8,
            "turnover_rate": 0.04,
            "circ_mv": 45_000_000_000,
            "breakout_ratio": 1.4,
            "ma100_breakout_days": 2,
        }
        theme_context = OpenClawThemeContext(
            source="openclaw",
            trade_date="2026-03-26",
            market="cn",
            themes=[
                ExternalTheme(
                    name="机器人",
                    heat_score=88.0,
                    confidence=0.85,
                    catalyst_summary="政策催化",
                    keywords=["机器人"],
                    evidence=[],
                )
            ],
            accepted_at=datetime.now().isoformat(),
        )

        enriched = self.enricher.enrich_snapshot(
            snapshot,
            theme_context=theme_context,
            boards=["机器人"],
        )

        self.assertTrue(enriched["is_hot_theme_stock"])
        self.assertGreater(enriched["extreme_strength_score"], 60)

    def test_enrich_snapshot_missing_circ_mv_does_not_get_small_cap_bonus(self) -> None:
        """Missing circ_mv should be treated as unknown, not as strongest small-cap signal."""
        snapshot = {
            "code": "000001",
            "name": "机器人",
            "close": 10.5,
            "above_ma100": True,
            "gap_breakaway": True,
            "is_limit_up": True,
            "turnover_rate": 5.0,
            "ma100_breakout_days": 3,
        }
        theme_context = OpenClawThemeContext(
            source="openclaw",
            trade_date="2026-03-26",
            market="cn",
            themes=[
                ExternalTheme(
                    name="机器人",
                    heat_score=90.0,
                    confidence=0.85,
                    catalyst_summary="政策催化",
                    keywords=["机器人"],
                    evidence=[],
                )
            ],
            accepted_at=datetime.now().isoformat(),
        )

        enriched = self.enricher.enrich_snapshot(
            snapshot,
            theme_context=theme_context,
            boards=["机器人"],
        )

        self.assertEqual(enriched["theme_leader_score"], 70)
        self.assertEqual(enriched["leader_score"], 70)

    def test_resolve_effective_scores_falls_back_to_base_when_theme_scores_zero(self) -> None:
        """有效分选择应支持题材分为 0 时回退基础分。"""
        leader_score, extreme_strength_score = self.enricher._resolve_effective_scores(
            base_leader_score=48.0,
            base_extreme_strength_score=61.0,
            theme_leader_score=0.0,
            theme_extreme_strength_score=0.0,
        )

        self.assertEqual((leader_score, extreme_strength_score), (48.0, 61.0))

    def test_enrich_snapshot_without_intraday_minutes_falls_back_to_ma100_reason(self) -> None:
        """Missing intraday timing should not auto-claim early limit-up."""
        snapshot = {
            "code": "000001",
            "name": "机器人",
            "close": 10.5,
            "above_ma100": True,
            "gap_breakaway": False,
            "is_limit_up": True,
            "turnover_rate": 3.0,
            "circ_mv": 75_000_000_000,
            "ma100_breakout_days": 3,
        }
        theme_context = OpenClawThemeContext(
            source="openclaw",
            trade_date="2026-03-26",
            market="cn",
            themes=[
                ExternalTheme(
                    name="机器人",
                    heat_score=90.0,
                    confidence=0.85,
                    catalyst_summary="政策催化",
                    keywords=["机器人"],
                    evidence=[],
                )
            ],
            accepted_at=datetime.now().isoformat(),
        )

        enriched = self.enricher.enrich_snapshot(
            snapshot,
            theme_context=theme_context,
            boards=["机器人"],
        )

        self.assertEqual(enriched["entry_reason"], "站上/刚突破MA100")


    def test_enrich_snapshot_uses_named_five_phase_structure(self) -> None:
        """Phase payload should only expose the formal five-stage keys and explanations."""
        snapshot = {
            "code": "000001",
            "name": "robotics",
            "close": 10.5,
            "above_ma100": True,
            "gap_breakaway": True,
            "pattern_123_low_trendline": False,
            "is_limit_up": True,
            "bottom_divergence_double_breakout": False,
            "volume_ratio": 1.5,
            "turnover_rate": 5.0,
            "circ_mv": 75_000_000_000,
            "breakout_ratio": 1.2,
            "ma100_breakout_days": 3,
            "ma100": 10.0,
        }
        theme_context = OpenClawThemeContext(
            source="openclaw",
            trade_date="2026-03-26",
            market="cn",
            themes=[
                ExternalTheme(
                    name="robotics",
                    heat_score=90.0,
                    confidence=0.85,
                    catalyst_summary="policy catalyst",
                    keywords=["robotics"],
                    evidence=[],
                )
            ],
            accepted_at=datetime.now().isoformat(),
        )

        enriched = self.enricher.enrich_snapshot(
            snapshot,
            theme_context=theme_context,
            boards=["robotics"],
        )

        self.assertEqual(
            set(enriched["phase_results"].keys()),
            {
                "phase1_market_and_theme",
                "phase2_leader_screen",
                "phase3_core_signal",
                "phase4_entry_readiness",
                "phase5_risk_controls",
            },
        )
        self.assertTrue(enriched["phase_results"]["phase1_market_and_theme"])
        self.assertTrue(enriched["phase_results"]["phase2_leader_screen"])
        self.assertTrue(enriched["phase_results"]["phase3_core_signal"])
        self.assertTrue(enriched["phase_results"]["phase4_entry_readiness"])
        self.assertTrue(enriched["phase_results"]["phase5_risk_controls"])

        self.assertEqual(len(enriched["phase_explanations"]), 5)
        self.assertEqual(
            enriched["phase_explanations"][0]["phase_key"],
            "phase1_market_and_theme",
        )
        self.assertIn("leader_score", enriched["phase_explanations"][1]["summary"])


if __name__ == "__main__":
    unittest.main()
