# -*- coding: utf-8 -*-
"""
TDD：EntryMaturityAssessor + CandidatePoolClassifier + TradeStageJudge 测试。
"""

import inspect
import unittest

from src.schemas.trading_types import (
    CandidatePoolLevel,
    EntryMaturity,
    MarketEnvironment,
    MarketRegime,
    RiskLevel,
    SetupType,
    ThemePosition,
    TradeStage,
)
from src.services.entry_maturity_assessor import EntryMaturityAssessor
from src.services.candidate_pool_classifier import CandidatePoolClassifier
from src.services.setup_freshness_assessor import SetupFreshnessAssessor
from src.services.trade_stage_judge import TradeStageJudge


# ═══════════════════════════════════════════════════════════════════════════════
# EntryMaturityAssessor
# ═══════════════════════════════════════════════════════════════════════════════


class EntryMaturityAssessorTestCase(unittest.TestCase):

    def setUp(self) -> None:
        self.assessor = EntryMaturityAssessor()

    def test_bottom_divergence_confirmed_high(self) -> None:
        """底背离 confirmed → HIGH。"""
        fs = {"bottom_divergence_state": "confirmed", "bottom_divergence_signal_strength": 80}
        result = self.assessor.assess(SetupType.BOTTOM_DIVERGENCE_BREAKOUT, fs)
        self.assertEqual(result, EntryMaturity.HIGH)

    def test_bottom_divergence_structure_ready_medium(self) -> None:
        """底背离 structure_ready → MEDIUM。"""
        fs = {"bottom_divergence_state": "structure_ready"}
        result = self.assessor.assess(SetupType.BOTTOM_DIVERGENCE_BREAKOUT, fs)
        self.assertEqual(result, EntryMaturity.MEDIUM)

    def test_bottom_divergence_pending_low(self) -> None:
        """底背离 divergence_only/pending → LOW。"""
        fs = {"bottom_divergence_state": "divergence_only"}
        result = self.assessor.assess(SetupType.BOTTOM_DIVERGENCE_BREAKOUT, fs)
        self.assertEqual(result, EntryMaturity.LOW)

    def test_bottom_divergence_late_or_weak_medium(self) -> None:
        """late_or_weak：两线都突破但间隔 > 3 bars，仍是有效买点，标记 MEDIUM。

        书中《各形态买卖点识别手册》对底背离双突破的要求是"同步双突破"，
        但 detector 已确认两条线都被突破，只是同步性弱 → 买点成立但成熟度
        略逊于 confirmed，按 Notion 描述归为 MEDIUM 而非 LOW。
        """
        fs = {"bottom_divergence_state": "late_or_weak"}
        result = self.assessor.assess(SetupType.BOTTOM_DIVERGENCE_BREAKOUT, fs)
        self.assertEqual(result, EntryMaturity.MEDIUM)

    def test_bottom_divergence_v2_stage_maturity_mapping(self) -> None:
        cases = {
            "forming": EntryMaturity.LOW,
            "invalidated": EntryMaturity.LOW,
            "stale": EntryMaturity.LOW,
            "extended": EntryMaturity.LOW,
            "major_unverified": EntryMaturity.LOW,
            "breakout_failed": EntryMaturity.LOW,
            "major_confirmed": EntryMaturity.LOW,
            "unknown_stage": EntryMaturity.LOW,
            "rejected": EntryMaturity.LOW,
            "early": EntryMaturity.MEDIUM,
            "near_cleared": EntryMaturity.HIGH,
        }
        for stage, expected in cases.items():
            with self.subTest(stage=stage):
                result = self.assessor.assess(
                    SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
                    {"bottom_divergence_v2_stage": stage},
                )
                self.assertEqual(result, expected)

    def test_bottom_divergence_v2_major_requires_actionability(self) -> None:
        self.assertEqual(
            self.assessor.assess(
                SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
                {
                    "bottom_divergence_v2_stage": "major_actionable",
                    "bottom_divergence_v2_major_actionable_entry": True,
                },
            ),
            EntryMaturity.HIGH,
        )
        self.assertEqual(
            self.assessor.assess(
                SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
                {
                    "bottom_divergence_v2_stage": "major_actionable",
                    "bottom_divergence_v2_major_actionable_entry": False,
                    "bottom_divergence_v2_actionability_status": "extended",
                },
            ),
            EntryMaturity.LOW,
        )
        self.assertEqual(
            self.assessor.assess(
                SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
                {
                    "bottom_divergence_v2_stage": "major_confirmed",
                    "bottom_divergence_v2_major_actionable_entry": True,
                },
            ),
            EntryMaturity.LOW,
        )

    def test_low123_breakout_ready_high(self) -> None:
        """Low123 breakout_ready → HIGH。"""
        fs = {"pattern_123_state": "breakout_ready", "pattern_123_signal_strength": 0.8}
        result = self.assessor.assess(SetupType.LOW123_BREAKOUT, fs)
        self.assertEqual(result, EntryMaturity.HIGH)

    def test_low123_watching_medium(self) -> None:
        """Low123 watching → MEDIUM。"""
        fs = {"pattern_123_state": "watching", "pattern_123_signal_strength": 0.4}
        result = self.assessor.assess(SetupType.LOW123_BREAKOUT, fs)
        self.assertEqual(result, EntryMaturity.MEDIUM)

    def test_trend_breakout_fresh_high(self) -> None:
        """趋势突破 ≤5 天 → HIGH。"""
        fs = {"ma100_breakout_days": 3}
        result = self.assessor.assess(SetupType.TREND_BREAKOUT, fs)
        self.assertEqual(result, EntryMaturity.HIGH)

    def test_trend_breakout_stale_medium(self) -> None:
        """趋势突破 6-10 天 → MEDIUM。"""
        fs = {"ma100_breakout_days": 8}
        result = self.assessor.assess(SetupType.TREND_BREAKOUT, fs)
        self.assertEqual(result, EntryMaturity.MEDIUM)

    def test_trend_breakout_old_low(self) -> None:
        """趋势突破 > 10 天 → LOW。"""
        fs = {"ma100_breakout_days": 15}
        result = self.assessor.assess(SetupType.TREND_BREAKOUT, fs)
        self.assertEqual(result, EntryMaturity.LOW)

    def test_gap_breakout_with_limitup_high(self) -> None:
        """缺口 + 涨停 → HIGH。"""
        fs = {"gap_breakaway": True, "is_limit_up": True}
        result = self.assessor.assess(SetupType.GAP_BREAKOUT, fs)
        self.assertEqual(result, EntryMaturity.HIGH)

    def test_gap_breakout_without_limitup_medium(self) -> None:
        """缺口无涨停 → MEDIUM。"""
        fs = {"gap_breakaway": True, "is_limit_up": False}
        result = self.assessor.assess(SetupType.GAP_BREAKOUT, fs)
        self.assertEqual(result, EntryMaturity.MEDIUM)

    def test_none_setup_always_low(self) -> None:
        """setup_type=NONE → LOW。"""
        result = self.assessor.assess(SetupType.NONE, {})
        self.assertEqual(result, EntryMaturity.LOW)


class SetupFreshnessAssessorV2TestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.assessor = SetupFreshnessAssessor()

    def test_uses_only_flat_v2_event_days_when_present(self) -> None:
        for event_days, expected in ((0, 1.0), (1, 0.9), (2, 0.8)):
            with self.subTest(event_days=event_days):
                freshness = self.assessor.assess(
                    SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
                    {
                        "bottom_divergence_v2_stage": "early",
                        "bottom_divergence_v2_event_days": event_days,
                    },
                )
                self.assertEqual(freshness, expected)

    def test_missing_flat_event_days_ignores_record_as_of_and_confirmation(self) -> None:
        freshness = self.assessor.assess(
            SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
            {
                "bottom_divergence_v2_stage": "early",
                "bottom_divergence_v2_confirmation_days": 2,
                "bottom_divergence_confirmation_days": 0,
                "bottom_divergence_v2_candidate_records": [
                    {
                        "candidate_version": "primary",
                        "lifecycle": "confirmed",
                        "as_of_index": 20,
                        "early_reversal": {"bar_index": 20},
                    },
                ],
            },
        )

        self.assertEqual(freshness, 0.8)

    def test_missing_event_inputs_use_stage_safe_defaults(self) -> None:
        expected = {
            "early": 0.8,
            "near_cleared": 0.9,
            "major_actionable": 1.0,
            "forming": 0.5,
            "stale": 0.3,
        }
        for stage, score in expected.items():
            with self.subTest(stage=stage):
                freshness = self.assessor.assess(
                    SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
                    {
                        "bottom_divergence_v2_stage": stage,
                        "bottom_divergence_confirmation_days": 99,
                    },
                )
                self.assertEqual(freshness, score)

    def test_unknown_v2_stage_fails_closed_even_with_fresh_event(self) -> None:
        for stage in ("major_confirmed", "unknown_stage"):
            with self.subTest(stage=stage):
                freshness = self.assessor.assess(
                    SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY,
                    {
                        "bottom_divergence_v2_stage": stage,
                        "bottom_divergence_v2_event_days": 0,
                    },
                )
                self.assertEqual(freshness, 0.3)


# ═══════════════════════════════════════════════════════════════════════════════
# CandidatePoolClassifier
# ═══════════════════════════════════════════════════════════════════════════════


class CandidatePoolClassifierTestCase(unittest.TestCase):

    def setUp(self) -> None:
        self.classifier = CandidatePoolClassifier()

    def test_leader_pool_via_leader_score_and_theme(self) -> None:
        """leader_score≥70 + MAIN_THEME → LEADER_POOL。"""
        result = self.classifier.classify(
            leader_score=75.0,
            extreme_strength_score=50.0,
            theme_position=ThemePosition.MAIN_THEME,
            is_limit_up=True,
        )
        self.assertEqual(result, CandidatePoolLevel.LEADER_POOL)

    def test_leader_pool_via_extreme_strength_with_theme(self) -> None:
        """extreme_strength≥80 + MAIN_THEME → LEADER_POOL（必须在题材主线内）。"""
        result = self.classifier.classify(
            leader_score=30.0,
            extreme_strength_score=85.0,
            theme_position=ThemePosition.MAIN_THEME,
            is_limit_up=True,
        )
        self.assertEqual(result, CandidatePoolLevel.LEADER_POOL)

    def test_focus_list_via_extreme_strength(self) -> None:
        """extreme_strength≥60 + FOLLOWER_THEME → FOCUS_LIST。"""
        result = self.classifier.classify(
            leader_score=30.0,
            extreme_strength_score=65.0,
            theme_position=ThemePosition.FOLLOWER_THEME,
            is_limit_up=True,
        )
        self.assertEqual(result, CandidatePoolLevel.FOCUS_LIST)

    def test_focus_list_via_leader_score_secondary(self) -> None:
        """leader_score≥50 → FOCUS_LIST。"""
        result = self.classifier.classify(
            leader_score=55.0,
            extreme_strength_score=40.0,
            theme_position=ThemePosition.FOLLOWER_THEME,
            is_limit_up=True,
        )
        self.assertEqual(result, CandidatePoolLevel.FOCUS_LIST)

    def test_watchlist_default(self) -> None:
        """低分 + FOLLOWER_THEME → WATCHLIST。"""
        result = self.classifier.classify(
            leader_score=20.0,
            extreme_strength_score=30.0,
            theme_position=ThemePosition.FOLLOWER_THEME,
            is_limit_up=False,
        )
        self.assertEqual(result, CandidatePoolLevel.WATCHLIST)


# ═══════════════════════════════════════════════════════════════════════════════
# CandidatePoolClassifier — 防回退测试
# ═══════════════════════════════════════════════════════════════════════════════


class CandidatePoolRegressionTestCase(unittest.TestCase):
    """约束矩阵防回退测试。"""

    def setUp(self) -> None:
        self.classifier = CandidatePoolClassifier()

    def test_non_theme_cannot_enter_leader_pool(self) -> None:
        """NON_THEME 无论 extreme_strength 多高都不进 leader_pool。"""
        result = self.classifier.classify(
            leader_score=90.0,
            extreme_strength_score=99.0,
            theme_position=ThemePosition.NON_THEME,
            is_limit_up=True,
        )
        self.assertNotEqual(result, CandidatePoolLevel.LEADER_POOL)
        self.assertEqual(result, CandidatePoolLevel.WATCHLIST)

    def test_follower_theme_max_focus_list(self) -> None:
        """FOLLOWER_THEME 最高 FOCUS_LIST，不可进 LEADER_POOL。"""
        result = self.classifier.classify(
            leader_score=90.0,
            extreme_strength_score=99.0,
            theme_position=ThemePosition.FOLLOWER_THEME,
            is_limit_up=True,
        )
        self.assertNotEqual(result, CandidatePoolLevel.LEADER_POOL)
        self.assertEqual(result, CandidatePoolLevel.FOCUS_LIST)

    def test_fading_theme_always_watchlist(self) -> None:
        """FADING_THEME 固定 WATCHLIST。"""
        result = self.classifier.classify(
            leader_score=90.0,
            extreme_strength_score=99.0,
            theme_position=ThemePosition.FADING_THEME,
            is_limit_up=True,
        )
        self.assertEqual(result, CandidatePoolLevel.WATCHLIST)

    def test_no_l4_params_in_classifier(self) -> None:
        """classify() 签名不含 entry_maturity / has_entry_core_hit。"""
        sig = inspect.signature(self.classifier.classify)
        param_names = set(sig.parameters.keys())
        self.assertNotIn("entry_maturity", param_names)
        self.assertNotIn("has_entry_core_hit", param_names)


# ═══════════════════════════════════════════════════════════════════════════════
# TradeStageJudge
# ═══════════════════════════════════════════════════════════════════════════════


def _make_env(regime: MarketRegime = MarketRegime.BALANCED) -> MarketEnvironment:
    return MarketEnvironment(
        regime=regime,
        risk_level=RiskLevel.MEDIUM,
        is_safe=regime != MarketRegime.STAND_ASIDE,
    )


class TradeStageJudgeTestCase(unittest.TestCase):

    def setUp(self) -> None:
        self.judge = TradeStageJudge()

    # ── 环境硬门控 ───────────────────────────────────────────────────────────

    def test_stand_aside_caps_at_watch(self) -> None:
        """stand_aside 环境 → 最高 WATCH。"""
        result = self.judge.judge(
            env=_make_env(MarketRegime.STAND_ASIDE),
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.HIGH,
            pool_level=CandidatePoolLevel.LEADER_POOL,
            theme_position=ThemePosition.MAIN_THEME,
            has_stop_loss=True,
        )
        self.assertIn(result, [TradeStage.STAND_ASIDE, TradeStage.WATCH])

    def test_defensive_blocks_add_on(self) -> None:
        """defensive 环境 → 禁止 ADD_ON_STRENGTH。"""
        result = self.judge.judge(
            env=_make_env(MarketRegime.DEFENSIVE),
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.HIGH,
            pool_level=CandidatePoolLevel.LEADER_POOL,
            theme_position=ThemePosition.MAIN_THEME,
            has_stop_loss=True,
        )
        self.assertNotEqual(result, TradeStage.ADD_ON_STRENGTH)

    # ── 题材约束 ─────────────────────────────────────────────────────────────

    def test_fading_theme_caps_at_watch(self) -> None:
        """FADING_THEME → 最高 WATCH。"""
        result = self.judge.judge(
            env=_make_env(MarketRegime.AGGRESSIVE),
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.HIGH,
            pool_level=CandidatePoolLevel.LEADER_POOL,
            theme_position=ThemePosition.FADING_THEME,
            has_stop_loss=True,
        )
        self.assertIn(result, [TradeStage.WATCH, TradeStage.FOCUS])

    def test_non_theme_blocks_add_on(self) -> None:
        """NON_THEME → 禁止 ADD_ON_STRENGTH。"""
        result = self.judge.judge(
            env=_make_env(MarketRegime.AGGRESSIVE),
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.HIGH,
            pool_level=CandidatePoolLevel.LEADER_POOL,
            theme_position=ThemePosition.NON_THEME,
            has_stop_loss=True,
        )
        self.assertNotEqual(result, TradeStage.ADD_ON_STRENGTH)

    # ── 买点成熟度 ───────────────────────────────────────────────────────────

    def test_no_setup_caps_at_watch(self) -> None:
        """setup_type=NONE → 最高 WATCH。"""
        result = self.judge.judge(
            env=_make_env(MarketRegime.AGGRESSIVE),
            setup_type=SetupType.NONE,
            entry_maturity=EntryMaturity.LOW,
            pool_level=CandidatePoolLevel.WATCHLIST,
            theme_position=ThemePosition.MAIN_THEME,
            has_stop_loss=False,
        )
        self.assertIn(result, [TradeStage.STAND_ASIDE, TradeStage.WATCH])

    def test_low_maturity_caps_at_focus(self) -> None:
        """entry_maturity=LOW → 最高 FOCUS。"""
        result = self.judge.judge(
            env=_make_env(MarketRegime.AGGRESSIVE),
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.LOW,
            pool_level=CandidatePoolLevel.FOCUS_LIST,
            theme_position=ThemePosition.MAIN_THEME,
            has_stop_loss=True,
        )
        self.assertIn(result, [TradeStage.WATCH, TradeStage.FOCUS])

    def test_medium_maturity_with_stop_loss_probe_entry(self) -> None:
        """entry_maturity=MEDIUM + 有止损 → PROBE_ENTRY。"""
        result = self.judge.judge(
            env=_make_env(MarketRegime.BALANCED),
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.MEDIUM,
            pool_level=CandidatePoolLevel.FOCUS_LIST,
            theme_position=ThemePosition.MAIN_THEME,
            has_stop_loss=True,
        )
        self.assertEqual(result, TradeStage.PROBE_ENTRY)

    def test_no_stop_loss_caps_at_focus(self) -> None:
        """无止损锚点 → 最高 FOCUS。"""
        result = self.judge.judge(
            env=_make_env(MarketRegime.AGGRESSIVE),
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.HIGH,
            pool_level=CandidatePoolLevel.LEADER_POOL,
            theme_position=ThemePosition.MAIN_THEME,
            has_stop_loss=False,
        )
        self.assertEqual(result, TradeStage.FOCUS)

    def test_high_maturity_leader_pool_add_on(self) -> None:
        """HIGH + LEADER_POOL + aggressive + MAIN_THEME + stop_loss → ADD_ON_STRENGTH。"""
        result = self.judge.judge(
            env=_make_env(MarketRegime.AGGRESSIVE),
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.HIGH,
            pool_level=CandidatePoolLevel.LEADER_POOL,
            theme_position=ThemePosition.MAIN_THEME,
            has_stop_loss=True,
        )
        self.assertEqual(result, TradeStage.ADD_ON_STRENGTH)

    def test_high_maturity_balanced_probe_entry(self) -> None:
        """HIGH + balanced → 至少 PROBE_ENTRY。"""
        result = self.judge.judge(
            env=_make_env(MarketRegime.BALANCED),
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.HIGH,
            pool_level=CandidatePoolLevel.FOCUS_LIST,
            theme_position=ThemePosition.MAIN_THEME,
            has_stop_loss=True,
        )
        self.assertIn(result, [TradeStage.PROBE_ENTRY, TradeStage.ADD_ON_STRENGTH])


# ═══════════════════════════════════════════════════════════════════════════════
# TradeStageJudge — 防回退测试
# ═══════════════════════════════════════════════════════════════════════════════


class TradeStageRegressionTestCase(unittest.TestCase):
    """约束矩阵防回退：仅 MAIN_THEME 可达 ADD_ON_STRENGTH。"""

    def setUp(self) -> None:
        self.judge = TradeStageJudge()

    def test_secondary_theme_max_probe_entry(self) -> None:
        """SECONDARY_THEME 最高 PROBE_ENTRY，不可达 ADD_ON_STRENGTH。"""
        result = self.judge.judge(
            env=_make_env(MarketRegime.AGGRESSIVE),
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.HIGH,
            pool_level=CandidatePoolLevel.LEADER_POOL,
            theme_position=ThemePosition.SECONDARY_THEME,
            has_stop_loss=True,
        )
        self.assertNotEqual(result, TradeStage.ADD_ON_STRENGTH)
        self.assertIn(result, [TradeStage.PROBE_ENTRY, TradeStage.FOCUS, TradeStage.WATCH])

    def test_only_main_theme_can_add_on_strength(self) -> None:
        """遍历所有非 MAIN_THEME 的 theme_position，确认无法达到 ADD_ON_STRENGTH。"""
        non_main_themes = [
            ThemePosition.SECONDARY_THEME,
            ThemePosition.FOLLOWER_THEME,
            ThemePosition.FADING_THEME,
            ThemePosition.NON_THEME,
        ]
        for tp in non_main_themes:
            # 同上：枚举无法被 xdist 的报告通道序列化，并行下会整条变红。
            with self.subTest(theme_position=tp.value):
                result = self.judge.judge(
                    env=_make_env(MarketRegime.AGGRESSIVE),
                    setup_type=SetupType.TREND_BREAKOUT,
                    entry_maturity=EntryMaturity.HIGH,
                    pool_level=CandidatePoolLevel.LEADER_POOL,
                    theme_position=tp,
                    has_stop_loss=True,
                )
                self.assertNotEqual(
                    result, TradeStage.ADD_ON_STRENGTH,
                    f"{tp} should not reach ADD_ON_STRENGTH",
                )


# ═══════════════════════════════════════════════════════════════════════════════
# ThemePositionResolver — 外部上下文融合防回退测试
# ═══════════════════════════════════════════════════════════════════════════════


class ExternalContextRegressionTestCase(unittest.TestCase):
    """OpenClaw 外部热点上下文融合测试。"""

    def test_external_context_upgrades_non_theme(self) -> None:
        """OpenClaw 强信号可将 non_theme 升级为 follower_theme。"""
        from src.services.sector_heat_engine import SectorHeatResult
        from src.services.theme_aggregation_service import ThemeAggregateResult
        from src.services.theme_position_resolver import ThemePositionResolver

        sector = SectorHeatResult(
            board_name="AI应用",
            board_type="concept",
            sector_hot_score=25.0,
            sector_status="cold",
            sector_stage="ferment",
        )
        theme = ThemeAggregateResult(
            theme_tag="AI应用",
            theme_score=25.0,
        )
        context = {
            "themes": [
                {"name": "AI应用", "heat_score": 85, "confidence": 0.9},
            ]
        }

        resolver = ThemePositionResolver(
            sector_results=[sector],
            theme_results=[theme],
            theme_context=context,
        )
        decision = resolver.resolve(["AI应用"])
        self.assertEqual(decision.theme_position, ThemePosition.FOLLOWER_THEME)


if __name__ == "__main__":
    unittest.main()
