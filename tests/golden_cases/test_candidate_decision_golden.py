import unittest
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd

from src.schemas.trading_types import (
    AiReviewDecision,
    CandidateDecision,
    CandidatePoolLevel,
    EntryMaturity,
    MarketEnvironment,
    MarketRegime,
    RiskLevel,
    SetupType,
    ThemeDecision,
    ThemePosition,
    TradePlan,
    TradeStage,
)
from src.services.candidate_decision_builder import CandidateDecisionBuilder
from src.services.five_layer_pipeline import FiveLayerPipeline
from src.services.screener_service import ScreeningCandidateRecord, ScreeningEvaluationResult
from src.services.ai_review_protocol import AiReviewProtocol
from src.services.setup_freshness_assessor import SetupFreshnessAssessor
from src.services.trade_plan_builder import TradePlanBuilder


class CandidateDecisionGoldenCase(unittest.TestCase):
    def test_probe_entry_payload_round_trip_golden_case(self) -> None:
        decision = CandidateDecision(
            code="600519",
            name="贵州茅台",
            rank=1,
            selected_for_ai=True,
            rule_score=92.5,
            rule_hits=["trend_aligned", "volume_expanding"],
            factor_snapshot={"close": 1520.0, "ma20": 1488.0, "ma100": 1380.0},
            matched_strategies=["ma100_60min_combined", "volume_breakout"],
            market_regime=MarketRegime.BALANCED,
            risk_level=RiskLevel.MEDIUM,
            theme_tag="白酒",
            theme_score=88.0,
            theme_position=ThemePosition.MAIN_THEME,
            theme_duration="expanding",
            trade_theme_stage="expanding",
            candidate_pool_level=CandidatePoolLevel.LEADER_POOL,
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.HIGH,
            setup_freshness=0.9,
            trade_stage=TradeStage.PROBE_ENTRY,
            trade_plan=TradePlan(
                initial_position="1/5仓",
                stop_loss_rule="跌破MA20止损",
                take_profit_plan="沿MA10移动止盈",
                invalidation_rule="3日未启动离场",
                risk_level=RiskLevel.MEDIUM,
                holding_expectation="1~2周波段",
                execution_note="围绕趋势延续与关键均线支撑执行，现价1520.00，MA20=1488.00，MA100=1380.00",
            ),
            ai_review=AiReviewDecision(
                ai_query_id="golden-query-1",
                ai_summary="趋势完整，等待回踩后试错。",
                ai_operation_advice="关注",
                ai_trade_stage=TradeStage.FOCUS,
                ai_reasoning="市场中性偏强，题材一致。",
                ai_confidence=0.78,
                ai_environment_ok=True,
                ai_theme_alignment=True,
                ai_entry_quality="high",
                stage_conflict=False,
            ),
        )

        payload = decision.to_payload()
        restored = CandidateDecision.from_payload(payload)

        self.assertEqual(payload["trade_stage"], "probe_entry")
        self.assertEqual(payload["setup_type"], "trend_breakout")
        self.assertEqual(payload["theme_tag"], "白酒")
        self.assertEqual(payload["setup_freshness"], 0.9)
        self.assertEqual(payload["trade_plan"]["execution_note"], decision.trade_plan.execution_note)
        self.assertEqual(payload["ai_review"]["ai_trade_stage"], "focus")
        self.assertEqual(restored.trade_stage, TradeStage.PROBE_ENTRY)
        self.assertEqual(restored.trade_plan.execution_note, decision.trade_plan.execution_note)
        self.assertEqual(restored.ai_review.ai_trade_stage, TradeStage.FOCUS)

    def test_ai_review_conflict_is_capped_by_market_regime_golden_case(self) -> None:
        protocol = AiReviewProtocol()

        result = protocol.parse_ai_response(
            ai_summary='{"suggested_stage":"add_on_strength","confidence":0.91,"reasoning":"强势延续","risk_flags":[],"summary":"可加仓","environment_ok":true,"theme_alignment":true,"entry_quality":"high"}',
            ai_operation_advice="加仓",
            rule_trade_stage="focus",
            market_regime="defensive",
        )

        self.assertEqual(result.ai_trade_stage, "probe_entry")
        self.assertTrue(result.stage_conflict)
        self.assertEqual(result.ai_entry_quality, "high")

    def test_setup_freshness_and_execution_note_golden_case(self) -> None:
        freshness = SetupFreshnessAssessor().assess(
            SetupType.TREND_BREAKOUT,
            {"ma100_breakout_days": 2, "close": 25.5, "ma20": 24.8, "ma100": 22.0},
        )
        plan = TradePlanBuilder().build(
            trade_stage=TradeStage.PROBE_ENTRY,
            setup_type=SetupType.TREND_BREAKOUT,
            entry_maturity=EntryMaturity.HIGH,
            risk_level=RiskLevel.MEDIUM,
            pool_level=CandidatePoolLevel.LEADER_POOL,
            factor_snapshot={"close": 25.5, "ma20": 24.8, "ma100": 22.0},
        )

        self.assertEqual(freshness, 0.9)
        self.assertIsNotNone(plan)
        self.assertIn("MA20=24.80", plan.execution_note)
        self.assertIn("MA100=22.00", plan.execution_note)

    def _run_pipeline_golden_case(self, *, is_limit_up: bool):
        """跑通完整五层链路并返回 (payload, result)。

        is_limit_up 单独参数化：它是 CandidatePoolClassifier 进 leader_pool
        的准入条件，正例与反例只差这一个字段，其余构造必须完全一致，
        否则证不出档位差异来自涨停与否。
        """
        snapshot_df = pd.DataFrame([
            {
                "code": "600519",
                "name": "贵州茅台",
                "close": 100.0,
                "pct_chg": 5.0,
                "ma5": 99.0,
                "ma10": 97.0,
                "ma20": 94.0,
                "ma60": 90.0,
                "ma100": 88.0,
                "volume_ratio": 1.8,
                "turnover_rate": 2.2,
                "above_ma100": True,
                "gap_breakaway": False,
            }
        ])
        selected = [
            ScreeningCandidateRecord(
                code="600519",
                name="贵州茅台",
                rank=1,
                rule_score=91.0,
                rule_hits=["trend_aligned", "volume_expanding"],
                factor_snapshot={
                    "close": 100.0,
                    "ma20": 94.0,
                    "ma100": 88.0,
                    "ma100_breakout_days": 2,
                    "leader_score": 85.0,
                    "extreme_strength_score": 62.0,
                    # 真实止损来源字段：trend_breakout 由 risk_params 兜底解析
                    "risk_params": {"stop_loss": 88.0},
                    "is_limit_up": is_limit_up,
                },
                matched_strategies=["ma100_60min_combined"],
                strategy_scores={"ma100_60min_combined": 91.0},
                setup_type="trend_breakout",
                strategy_family="trend",
            )
        ]

        class _StubScreenerService:
            def evaluate(self, snapshot_df, prefiltered_rules=None):
                return ScreeningEvaluationResult(selected=selected, rejected=[])

        class _FakeThemeResolver:
            def __init__(self, *args, **kwargs):
                self.identified_themes = []

            def get_main_theme_boards(self):
                return set()

            def resolve(self, stock_boards):
                return ThemeDecision(
                    theme_tag="白酒",
                    theme_score=88.0,
                    theme_position=ThemePosition.MAIN_THEME,
                    leader_score=86.0,
                    sector_strength=81.0,
                    theme_duration="3d",
                    trade_theme_stage="expand",
                    leader_stocks=["600519"],
                    front_stocks=["600519"],
                )

        db_mock = MagicMock()
        db_mock.batch_get_instrument_board_names.return_value = {"600519": ["白酒"]}

        market_env = MarketEnvironment(
            regime=MarketRegime.BALANCED,
            risk_level=RiskLevel.MEDIUM,
            index_price=3200.0,
            index_ma100=3100.0,
            is_safe=True,
            message="环境中性偏强",
        )
        guard_result = MagicMock(is_safe=True, index_price=3200.0, index_ma100=3100.0, message="guard ok")

        with patch("src.services.theme_position_resolver.ThemePositionResolver", _FakeThemeResolver):
            result = FiveLayerPipeline().run(
                snapshot_df=snapshot_df,
                trade_date=date(2026, 3, 31),
                market_env=market_env,
                guard_result=guard_result,
                screener_service=_StubScreenerService(),
                candidate_limit=1,
                db_manager=db_mock,
                skill_manager=None,
            )

        decision = CandidateDecisionBuilder.build_initial(result.candidates)[0]
        return decision.to_payload(), result

    def test_pipeline_golden_case_builds_complete_candidate_decision(self) -> None:
        payload, result = self._run_pipeline_golden_case(is_limit_up=True)

        self.assertEqual(payload["market_regime"], "balanced")
        self.assertTrue(payload["environment_ok"])
        self.assertEqual(payload["theme_tag"], "白酒")
        self.assertEqual(payload["theme_position"], "main_theme")
        self.assertEqual(payload["candidate_pool_level"], "leader_pool")
        self.assertEqual(payload["trade_stage"], "add_on_strength")
        self.assertEqual(payload["setup_freshness"], 0.9)
        self.assertEqual(payload["strategy_scores"], {"ma100_60min_combined": 91.0})
        self.assertEqual(payload["trade_plan"]["execution_note"], "围绕趋势延续与关键均线支撑执行，现价100.00，MA20=94.00，MA100=88.00")
        self.assertEqual(result.decision_context["market_environment"]["index_price"], 3200.0)

    def test_non_limit_up_main_theme_stays_in_focus_list(self) -> None:
        """反例：主线题材但未涨停，只能到 focus_list。

        这条钉住的是 CandidatePoolClassifier 的核心准入规则。
        没有它，分类器把涨停条件删掉也不会有任何测试变红。
        """
        payload, _ = self._run_pipeline_golden_case(is_limit_up=False)

        self.assertEqual(payload["theme_position"], "main_theme")
        self.assertEqual(payload["candidate_pool_level"], "focus_list")

