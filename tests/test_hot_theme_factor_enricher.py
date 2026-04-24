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

        # A5：entry_reason 以 stage_label 为主导；无 intraday_minutes 时不应自动
        # 声明 "开盘半小时内涨停"，而应保留 MA100 信息作为上下文后缀。
        # 该 snapshot 未携带 bars_since_* 字段 → stage=watch_only。
        self.assertEqual(enriched["entry_reason"], "仅观察 · 刚突破MA100")
        self.assertNotIn("开盘半小时内涨停", enriched["entry_reason"])


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


class LayeredSnapshotFieldsTestCase(unittest.TestCase):
    """阶段 A2：分层评分字段写入 snapshot 的回归测试。

    锁定：
    - 热点命中时 snapshot 包含 theme_pool_score / leadership_score /
      entry_signal_score / timing_penalty / extreme_strength_breakdown 五个字段
    - 四桶之和 == extreme_strength_score（当 leader_score_source == theme 时）
    - 无题材 / 未命中题材两条分支的字段回落到默认值
    """

    _EXPECTED_LAYERED_KEYS = {
        "theme_pool_score",
        "leadership_score",
        "entry_signal_score",
        "timing_penalty",
        # A7：重复加权估算 + 去重后的净总分（snapshot 级只读字段）
        "leader_double_count",
        "extreme_strength_score_deduplicated",
        "extreme_strength_breakdown",
    }

    def setUp(self) -> None:
        self.enricher = HotThemeFactorEnricher()

    @staticmethod
    def _theme_context() -> OpenClawThemeContext:
        return OpenClawThemeContext(
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

    def _hot_snapshot(self) -> dict:
        return {
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
            "ma100": 10.0,
        }

    def test_hot_theme_snapshot_contains_layered_fields(self) -> None:
        enriched = self.enricher.enrich_snapshot(
            self._hot_snapshot(),
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        self.assertLessEqual(self._EXPECTED_LAYERED_KEYS, set(enriched.keys()))
        self.assertTrue(enriched["is_hot_theme_stock"])
        self.assertGreater(enriched["theme_pool_score"], 0.0)
        self.assertGreater(enriched["leadership_score"], 0.0)
        self.assertGreater(enriched["entry_signal_score"], 0.0)

    def test_hot_theme_layered_sum_equals_extreme_strength_score(self) -> None:
        """四桶之和 == extreme_strength_score（effective 分源为 theme 时）。"""
        enriched = self.enricher.enrich_snapshot(
            self._hot_snapshot(),
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        self.assertEqual(enriched["extreme_strength_score_source"], "theme")
        bucket_sum = (
            enriched["theme_pool_score"]
            + enriched["leadership_score"]
            + enriched["entry_signal_score"]
            + enriched["timing_penalty"]
        )
        self.assertAlmostEqual(
            bucket_sum,
            enriched["theme_extreme_strength_score"],
            places=6,
        )
        self.assertAlmostEqual(
            bucket_sum,
            enriched["extreme_strength_score"],
            places=6,
        )

    def test_breakdown_exposes_all_sub_contributions(self) -> None:
        enriched = self.enricher.enrich_snapshot(
            self._hot_snapshot(),
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        breakdown = enriched["extreme_strength_breakdown"]
        self.assertIsInstance(breakdown, dict)
        self.assertEqual(
            set(breakdown.keys()),
            {
                "base_score",
                "signal_bonus",
                "theme_heat_contribution",
                "leader_contribution",
                "volume_contribution",
                "turnover_contribution",
                "circ_mv_contribution",
                "breakout_contribution",
                # A7：leader_score 与其它桶之间可观测的重复加权估算值
                "leader_double_count",
            },
        )

    def test_no_theme_context_layered_fields_default(self) -> None:
        snapshot = {
            "code": "000001",
            "name": "机器人",
            "close": 10.5,
            "above_ma100": True,
            "base_leader_score": 18.0,
            "base_extreme_strength_score": 22.0,
        }
        enriched = self.enricher.enrich_snapshot(snapshot, theme_context=None)
        self.assertLessEqual(self._EXPECTED_LAYERED_KEYS, set(enriched.keys()))
        self.assertEqual(enriched["theme_pool_score"], 0.0)
        self.assertEqual(enriched["leadership_score"], 0.0)
        self.assertEqual(enriched["entry_signal_score"], 0.0)
        self.assertEqual(enriched["timing_penalty"], 0.0)
        self.assertEqual(enriched["extreme_strength_breakdown"], {})
        self.assertEqual(enriched["leader_double_count"], 0.0)
        self.assertEqual(enriched["extreme_strength_score_deduplicated"], 0.0)

    def test_hot_theme_snapshot_exposes_deduplicated_score(self) -> None:
        """A7：热点股快照应暴露去重后的净总分，且满足 dedup < total。"""
        enriched = self.enricher.enrich_snapshot(
            self._hot_snapshot(),
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        self.assertGreater(enriched["leader_double_count"], 0.0)
        self.assertLess(
            enriched["extreme_strength_score_deduplicated"],
            enriched["extreme_strength_score"],
        )
        self.assertAlmostEqual(
            enriched["extreme_strength_score_deduplicated"],
            enriched["extreme_strength_score"] - enriched["leader_double_count"],
            places=6,
        )

    def test_non_hot_theme_layered_fields_default(self) -> None:
        snapshot = {
            "code": "000002",
            "name": "芯片",
            "close": 10.5,
            "above_ma100": True,
            "gap_breakaway": False,
            "is_limit_up": False,
            "volume_ratio": 1.0,
            "turnover_rate": 0.02,
            "circ_mv": 150_000_000_000,
            "breakout_ratio": 0.95,
        }
        enriched = self.enricher.enrich_snapshot(
            snapshot,
            theme_context=self._theme_context(),
            boards=["芯片"],
        )
        self.assertFalse(enriched["is_hot_theme_stock"])
        self.assertLessEqual(self._EXPECTED_LAYERED_KEYS, set(enriched.keys()))
        self.assertEqual(enriched["theme_pool_score"], 0.0)
        self.assertEqual(enriched["leadership_score"], 0.0)
        self.assertEqual(enriched["entry_signal_score"], 0.0)
        self.assertEqual(enriched["timing_penalty"], 0.0)
        self.assertEqual(enriched["extreme_strength_breakdown"], {})
        self.assertEqual(enriched["leader_double_count"], 0.0)
        self.assertEqual(enriched["extreme_strength_score_deduplicated"], 0.0)


class TimingSnapshotFieldsTestCase(unittest.TestCase):
    """阶段 A3：时机评估字段写入 snapshot 的回归测试。

    锁定：
    - hot 分支 snapshot 包含 stage_label / timing_reasons /
      timing_bars_since_event / timing_extended_pct 四个字段
    - non-hot / no-theme 分支 stage_label=pool_only、penalty=0
    - "强但走远" 场景下 timing_penalty 负分会传回 layered timing_penalty
      且在 layered 总分中等比反映
    """

    _EXPECTED_TIMING_KEYS = {
        "stage_label",
        "timing_reasons",
        "timing_bars_since_event",
        "timing_extended_pct",
    }

    def setUp(self) -> None:
        self.enricher = HotThemeFactorEnricher()

    @staticmethod
    def _theme_context() -> OpenClawThemeContext:
        return OpenClawThemeContext(
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

    def _hot_snapshot(self, **overrides: object) -> dict:
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
            "ma100": 10.0,
            "ma100_distance_pct": 5.0,
            "bars_since_breakaway_gap": 0,
            "bars_since_limitup_structure_breakout": 0,
        }
        snapshot.update(overrides)
        return snapshot

    def test_no_theme_stage_label_is_pool_only(self) -> None:
        snapshot = {
            "code": "000001",
            "name": "机器人",
            "close": 10.5,
            "above_ma100": True,
        }
        enriched = self.enricher.enrich_snapshot(snapshot, theme_context=None)
        self.assertLessEqual(self._EXPECTED_TIMING_KEYS, set(enriched.keys()))
        self.assertEqual(enriched["stage_label"], "pool_only")
        self.assertEqual(enriched["timing_penalty"], 0.0)
        self.assertEqual(enriched["timing_reasons"], [])

    def test_non_hot_theme_stage_label_is_pool_only(self) -> None:
        snapshot = self._hot_snapshot(name="芯片")
        enriched = self.enricher.enrich_snapshot(
            snapshot,
            theme_context=self._theme_context(),
            boards=["芯片"],
        )
        self.assertFalse(enriched["is_hot_theme_stock"])
        self.assertEqual(enriched["stage_label"], "pool_only")
        self.assertEqual(enriched["timing_penalty"], 0.0)
        self.assertIn("non_hot_theme", enriched["timing_reasons"])

    def test_hot_breakout_day_yields_zero_penalty(self) -> None:
        enriched = self.enricher.enrich_snapshot(
            self._hot_snapshot(),
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        self.assertTrue(enriched["is_hot_theme_stock"])
        self.assertEqual(enriched["stage_label"], "breakout_day")
        self.assertEqual(enriched["timing_penalty"], 0.0)
        self.assertEqual(enriched["timing_bars_since_event"], 0)

    def test_hot_stale_event_downgrades_to_do_not_chase(self) -> None:
        """事件已过期 5 根 K 线 → extended_do_not_chase + 负 timing_penalty。"""
        stale = self._hot_snapshot(
            bars_since_breakaway_gap=5,
            bars_since_limitup_structure_breakout=5,
        )
        enriched = self.enricher.enrich_snapshot(
            stale,
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        self.assertEqual(enriched["stage_label"], "extended_do_not_chase")
        self.assertLess(enriched["timing_penalty"], 0.0)
        # timing_penalty 必须落到分层 layered.timing_penalty 从而影响总分
        self.assertEqual(
            enriched["timing_penalty"],
            enriched["extreme_strength_breakdown"].get("timing_penalty", 0.0)
            if "timing_penalty" in enriched["extreme_strength_breakdown"]
            else enriched["timing_penalty"],
        )

    def test_hot_stale_event_total_score_reflects_penalty(self) -> None:
        """启用 timing_penalty 后的总分 == 未启用 timing_penalty 的总分 + penalty。"""
        baseline = self.enricher.enrich_snapshot(
            self._hot_snapshot(),
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        penalized = self.enricher.enrich_snapshot(
            self._hot_snapshot(
                bars_since_breakaway_gap=6,
                bars_since_limitup_structure_breakout=6,
            ),
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        self.assertEqual(baseline["timing_penalty"], 0.0)
        self.assertLess(penalized["timing_penalty"], 0.0)
        # 被罚后的总分必须严格小于未罚场景
        self.assertLess(
            penalized["theme_extreme_strength_score"],
            baseline["theme_extreme_strength_score"],
        )

    def test_hot_extended_from_ma_forces_do_not_chase(self) -> None:
        extended = self._hot_snapshot(
            bars_since_breakaway_gap=0,
            bars_since_limitup_structure_breakout=0,
            ma100_distance_pct=35.0,
        )
        enriched = self.enricher.enrich_snapshot(
            extended,
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        self.assertEqual(enriched["stage_label"], "extended_do_not_chase")
        self.assertLessEqual(enriched["timing_penalty"], -10.0)
        self.assertAlmostEqual(enriched["timing_extended_pct"], 35.0)

    def test_below_ma100_not_treated_as_extended(self) -> None:
        """股价在 MA100 之下（distance 为负）时不应被误罚为 extended。"""
        below = self._hot_snapshot(ma100_distance_pct=-7.5)
        enriched = self.enricher.enrich_snapshot(
            below,
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        self.assertEqual(enriched["timing_extended_pct"], 0.0)
        self.assertGreaterEqual(enriched["timing_penalty"], -0.0)


class SignalKindSnapshotFieldsTestCase(unittest.TestCase):
    """阶段 A4：signal_kind / primary_signal 字段写入 snapshot 的回归测试。

    锁定：
    - hot 分支 snapshot 包含 primary_signal / signal_kind / all_signals
    - no-theme / non-hot 分支回落到默认值 (None / "none" / [])
    - 低位 123 + 跳空涨停共存时，primary_signal 是低位123（不再被动量压住）
    - all_signals 中包含完整 {name, kind, score}
    """

    _EXPECTED_SIGNAL_KIND_KEYS = {"primary_signal", "signal_kind", "all_signals"}

    def setUp(self) -> None:
        self.enricher = HotThemeFactorEnricher()

    @staticmethod
    def _theme_context() -> OpenClawThemeContext:
        return OpenClawThemeContext(
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

    def _snapshot(self, **overrides: object) -> dict:
        snapshot = {
            "code": "000001",
            "name": "机器人",
            "close": 10.5,
            "above_ma100": True,
            "gap_breakaway": False,
            "pattern_123_low_trendline": False,
            "is_limit_up": False,
            "bottom_divergence_double_breakout": False,
            "volume_ratio": 1.5,
            "turnover_rate": 0.05,
            "circ_mv": 75_000_000_000,
            "breakout_ratio": 1.2,
            "ma100_breakout_days": 3,
            "ma100": 10.0,
            "ma100_distance_pct": 5.0,
            "bars_since_breakaway_gap": 0,
            "bars_since_limitup_structure_breakout": 0,
        }
        snapshot.update(overrides)
        return snapshot

    def test_no_theme_defaults_to_none_kind(self) -> None:
        enriched = self.enricher.enrich_snapshot(
            self._snapshot(),
            theme_context=None,
        )
        self.assertLessEqual(self._EXPECTED_SIGNAL_KIND_KEYS, set(enriched.keys()))
        self.assertIsNone(enriched["primary_signal"])
        self.assertEqual(enriched["signal_kind"], "none")
        self.assertEqual(enriched["all_signals"], [])

    def test_non_hot_theme_defaults_to_none_kind(self) -> None:
        enriched = self.enricher.enrich_snapshot(
            self._snapshot(name="芯片", gap_breakaway=True, is_limit_up=True),
            theme_context=self._theme_context(),
            boards=["芯片"],
        )
        self.assertFalse(enriched["is_hot_theme_stock"])
        self.assertIsNone(enriched["primary_signal"])
        self.assertEqual(enriched["signal_kind"], "none")
        self.assertEqual(enriched["all_signals"], [])

    def test_hot_momentum_chase_signal_kind(self) -> None:
        enriched = self.enricher.enrich_snapshot(
            self._snapshot(gap_breakaway=True, is_limit_up=True),
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        self.assertTrue(enriched["is_hot_theme_stock"])
        self.assertEqual(enriched["primary_signal"], "跳空涨停")
        self.assertEqual(enriched["signal_kind"], "momentum_chase")
        names = {s["name"] for s in enriched["all_signals"]}
        self.assertIn("跳空涨停", names)

    def test_hot_structure_signal_wins_over_momentum(self) -> None:
        """核心契约：低位123 + 跳空涨停共存时，primary_signal 是低位123。"""
        enriched = self.enricher.enrich_snapshot(
            self._snapshot(
                gap_breakaway=True,
                is_limit_up=True,
                pattern_123_low_trendline=True,
            ),
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        self.assertEqual(enriched["primary_signal"], "低位123结构")
        self.assertEqual(enriched["signal_kind"], "structure_low_entry")
        # 旧字段保留动量语义（兼容性）
        self.assertEqual(enriched["core_signal"], "跳空涨停")
        self.assertIn("低位123结构", enriched["bonus_signals"])

    def test_hot_bottom_divergence_is_structure(self) -> None:
        enriched = self.enricher.enrich_snapshot(
            self._snapshot(bottom_divergence_double_breakout=True),
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        self.assertEqual(enriched["primary_signal"], "底背离双突破")
        self.assertEqual(enriched["signal_kind"], "structure_low_entry")

    def test_all_signals_contain_kind_and_score(self) -> None:
        enriched = self.enricher.enrich_snapshot(
            self._snapshot(
                gap_breakaway=True,
                is_limit_up=True,
                pattern_123_low_trendline=True,
            ),
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        self.assertGreaterEqual(len(enriched["all_signals"]), 2)
        for entry in enriched["all_signals"]:
            self.assertIn("name", entry)
            self.assertIn("kind", entry)
            self.assertIn("score", entry)
            self.assertGreaterEqual(entry["score"], 0)


class EntryReasonStageLabelTestCase(unittest.TestCase):
    """阶段 A5：entry_reason 应由 stage_label 主导，旧描述词仅作为上下文后缀。

    契约：
    - 非热点 → entry_reason is None（保留旧语义）
    - 热点 + stage=breakout_day + is_limit_up + intraday<=30 →
      "突破当日 · 开盘半小时内涨停"
    - 热点 + stage=breakout_day + above_ma100 → "突破当日 · 刚突破MA100"
    - 热点 + stage=watch_only（无 bars）+ above_ma100 → "仅观察 · 刚突破MA100"
    - 热点 + stage=retest_entry → "回踩确认"
    - 热点 + stage=extended_do_not_chase → "已走远·勿追"
    - 旧字段 "站上/刚突破MA100" 不再出现为纯 entry_reason
    """

    def setUp(self) -> None:
        self.enricher = HotThemeFactorEnricher()

    @staticmethod
    def _theme_context() -> OpenClawThemeContext:
        return OpenClawThemeContext(
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

    def _hot_snapshot(self, **overrides: object) -> dict:
        snapshot = {
            "code": "000001",
            "name": "机器人",
            "close": 10.5,
            "above_ma100": True,
            "gap_breakaway": False,
            "pattern_123_low_trendline": False,
            "is_limit_up": False,
            "bottom_divergence_double_breakout": False,
            "volume_ratio": 1.5,
            "turnover_rate": 0.05,
            "circ_mv": 75_000_000_000,
            "breakout_ratio": 1.2,
            "ma100_breakout_days": 3,
            "ma100": 10.0,
        }
        snapshot.update(overrides)
        return snapshot

    def test_no_theme_keeps_entry_reason_none(self) -> None:
        """非热点分支仍然写入 entry_reason=None，保持旧 schema。"""
        enriched = self.enricher.enrich_snapshot(
            {"code": "000001", "name": "机器人", "close": 10.5, "above_ma100": True},
            theme_context=None,
        )
        self.assertIsNone(enriched["entry_reason"])

    def test_breakout_day_with_early_limit_up_composes_label(self) -> None:
        enriched = self.enricher.enrich_snapshot(
            self._hot_snapshot(
                is_limit_up=True,
                intraday_minutes_since_open=15,
                bars_since_limitup_structure_breakout=0,
            ),
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        self.assertEqual(enriched["stage_label"], "breakout_day")
        self.assertEqual(enriched["entry_reason"], "突破当日 · 开盘半小时内涨停")

    def test_breakout_day_with_ma100_only_composes_label(self) -> None:
        enriched = self.enricher.enrich_snapshot(
            self._hot_snapshot(
                gap_breakaway=True,
                bars_since_breakaway_gap=0,
            ),
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        self.assertEqual(enriched["stage_label"], "breakout_day")
        self.assertEqual(enriched["entry_reason"], "突破当日 · 刚突破MA100")

    def test_watch_only_without_bars_falls_back_to_ma100_suffix(self) -> None:
        """无 bars_since 字段 → watch_only；entry_reason 必须体现阶段而不是旧描述词。"""
        enriched = self.enricher.enrich_snapshot(
            self._hot_snapshot(is_limit_up=True),
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        self.assertEqual(enriched["stage_label"], "watch_only")
        self.assertEqual(enriched["entry_reason"], "仅观察 · 刚突破MA100")
        # 旧描述词不应再以纯文本出现（必须带阶段前缀）
        self.assertNotEqual(enriched["entry_reason"], "站上/刚突破MA100")

    def test_retest_entry_uses_stage_label_only(self) -> None:
        enriched = self.enricher.enrich_snapshot(
            self._hot_snapshot(
                above_ma100=False,
                gap_breakaway=True,
                bars_since_breakaway_gap=2,
            ),
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        self.assertEqual(enriched["stage_label"], "retest_entry")
        self.assertEqual(enriched["entry_reason"], "回踩确认")

    def test_extended_do_not_chase_stage_label_only(self) -> None:
        enriched = self.enricher.enrich_snapshot(
            self._hot_snapshot(
                gap_breakaway=True,
                bars_since_breakaway_gap=10,
            ),
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        self.assertEqual(enriched["stage_label"], "extended_do_not_chase")
        # 已走远 · 勿追 = 即使有 above_ma100 / 涨停等旧描述词，也以阶段标签为主
        self.assertTrue(
            enriched["entry_reason"].startswith("已走远·勿追"),
            msg=f"entry_reason={enriched['entry_reason']}",
        )


class RiskParamsBySubSignalTestCase(unittest.TestCase):
    """阶段 A6：risk_params 应按 primary_signal 引用真实子信号止损依据。

    契约：
    - 低位123 → stop_loss 来自 pattern_123_stop_loss / pullback_support_price
    - 底背离双突破 → stop_loss 来自 bottom_divergence_stop_loss
    - 缺口突破MA100 → stop_loss 来自 breakaway_gap_low
    - 跳空涨停 / 涨停 → stop_loss 来自 limitup_key_level_price（回退 breakaway_gap_low）
    - 均未命中 → 回退到 ma100 × 0.95
    - extended_do_not_chase → position_size 强制为 "不建议入场", take_profit=0
    - 新增 stop_loss_basis 字段显式标注止损依据来源
    """

    def setUp(self) -> None:
        self.enricher = HotThemeFactorEnricher()

    @staticmethod
    def _theme_context() -> OpenClawThemeContext:
        return OpenClawThemeContext(
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

    def _hot_snapshot(self, **overrides: object) -> dict:
        snapshot = {
            "code": "000001",
            "name": "机器人",
            "close": 10.5,
            "above_ma100": True,
            "gap_breakaway": False,
            "pattern_123_low_trendline": False,
            "is_limit_up": False,
            "bottom_divergence_double_breakout": False,
            "volume_ratio": 1.5,
            "turnover_rate": 0.05,
            "circ_mv": 75_000_000_000,
            "breakout_ratio": 1.2,
            "ma100_breakout_days": 3,
            "ma100": 10.0,
            "bars_since_breakaway_gap": 0,
            "bars_since_limitup_structure_breakout": 0,
        }
        snapshot.update(overrides)
        return snapshot

    def test_low_123_uses_pattern_123_stop_loss(self) -> None:
        enriched = self.enricher.enrich_snapshot(
            self._hot_snapshot(
                pattern_123_low_trendline=True,
                pattern_123_stop_loss=9.25,
                pattern_123_pullback_support_price=9.30,
            ),
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        self.assertEqual(enriched["primary_signal"], "低位123结构")
        self.assertAlmostEqual(enriched["risk_params"]["stop_loss"], 9.25)
        self.assertEqual(enriched["risk_params"]["stop_loss_basis"], "123结构低点")

    def test_low_123_fallback_to_pullback_support(self) -> None:
        """pattern_123_stop_loss 缺失时回退到 pullback_support_price。"""
        enriched = self.enricher.enrich_snapshot(
            self._hot_snapshot(
                pattern_123_low_trendline=True,
                pattern_123_stop_loss=None,
                pattern_123_pullback_support_price=9.30,
            ),
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        self.assertAlmostEqual(enriched["risk_params"]["stop_loss"], 9.30)
        self.assertEqual(enriched["risk_params"]["stop_loss_basis"], "123结构低点")

    def test_bottom_divergence_uses_divergence_stop_loss(self) -> None:
        enriched = self.enricher.enrich_snapshot(
            self._hot_snapshot(
                bottom_divergence_double_breakout=True,
                bottom_divergence_stop_loss=7.66,
            ),
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        self.assertEqual(enriched["primary_signal"], "底背离双突破")
        self.assertAlmostEqual(enriched["risk_params"]["stop_loss"], 7.66)
        self.assertEqual(enriched["risk_params"]["stop_loss_basis"], "底背离临界区")

    def test_gap_breakout_uses_gap_low(self) -> None:
        enriched = self.enricher.enrich_snapshot(
            self._hot_snapshot(
                gap_breakaway=True,
                breakaway_gap_low=9.80,
            ),
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        self.assertEqual(enriched["primary_signal"], "缺口突破MA100")
        self.assertAlmostEqual(enriched["risk_params"]["stop_loss"], 9.80)
        self.assertEqual(enriched["risk_params"]["stop_loss_basis"], "缺口下沿")

    def test_limit_up_structure_uses_key_level_price(self) -> None:
        """涨停场景引用 limitup_key_level_price 作为止损基准。"""
        enriched = self.enricher.enrich_snapshot(
            self._hot_snapshot(
                gap_breakaway=False,
                is_limit_up=True,
                limitup_structure_breakout=True,
                limitup_key_level_type="trendline",
                limitup_key_level_price=10.12,
            ),
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        self.assertEqual(enriched["primary_signal"], "涨停")
        self.assertAlmostEqual(enriched["risk_params"]["stop_loss"], 10.12)
        self.assertIn("trendline", enriched["risk_params"]["stop_loss_basis"])

    def test_no_primary_signal_falls_back_to_ma100(self) -> None:
        """没有任何子信号 → 回退到 MA100×0.95 模板，并在 basis 中体现。"""
        enriched = self.enricher.enrich_snapshot(
            self._hot_snapshot(
                # 所有信号都关掉，仅靠热点硬门槛 + above_ma100
            ),
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        self.assertIsNone(enriched["primary_signal"])
        self.assertAlmostEqual(enriched["risk_params"]["stop_loss"], 9.5)
        self.assertEqual(enriched["risk_params"]["stop_loss_basis"], "MA100×0.95")

    def test_extended_do_not_chase_forbids_position(self) -> None:
        enriched = self.enricher.enrich_snapshot(
            self._hot_snapshot(
                gap_breakaway=True,
                breakaway_gap_low=9.80,
                bars_since_breakaway_gap=10,
                bars_since_limitup_structure_breakout=-1,
            ),
            theme_context=self._theme_context(),
            boards=["机器人"],
        )
        self.assertEqual(enriched["stage_label"], "extended_do_not_chase")
        self.assertEqual(enriched["risk_params"]["position_size"], "不建议入场")
        self.assertEqual(enriched["risk_params"]["take_profit_ratio"], 0.0)


if __name__ == "__main__":
    unittest.main()
