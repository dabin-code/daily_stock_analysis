import unittest

from api.v1.schemas.screening import ScreeningCandidateItem, ScreeningRunResponse
from src.schemas.trading_types import (
    AiReviewDecision,
    CandidateDecision,
    CandidatePoolLevel,
    MarketRegime,
    RiskLevel,
    SetupType,
    StrategyFamily,
    ThemePosition,
    TradeStage,
)


class ScreeningApiSchemaTestCase(unittest.TestCase):
    def test_screening_candidate_item_preserves_candidate_decision_contract(self) -> None:
        decision = CandidateDecision(
            code="600519",
            name="贵州茅台",
            rank=1,
            selected_for_ai=True,
            rule_score=88.0,
            strategy_scores={"volume_breakout": 88.0},
            market_regime=MarketRegime.BALANCED,
            risk_level=RiskLevel.MEDIUM,
            environment_ok=True,
            index_price=3200.0,
            index_ma100=3100.0,
            theme_position=ThemePosition.MAIN_THEME,
            leader_score=82.0,
            candidate_pool_level=CandidatePoolLevel.LEADER_POOL,
            relative_strength_market=1.2,
            relative_strength_sector=1.4,
            setup_type=SetupType.TREND_BREAKOUT,
            strategy_family=StrategyFamily.TREND,
            trade_stage=TradeStage.PROBE_ENTRY,
            ai_review=AiReviewDecision(
                ai_query_id="ai-001",
                ai_summary="趋势延续，题材匹配。",
                ai_operation_advice="关注",
                ai_trade_stage=TradeStage.FOCUS,
                ai_reasoning="环境允许，等待确认。",
                ai_confidence=0.81,
                ai_environment_ok=True,
                ai_theme_alignment=True,
                ai_entry_quality="high",
                stage_conflict=False,
            ),
        )

        item = ScreeningCandidateItem(**decision.to_payload())

        self.assertEqual(item.strategy_family, "trend")
        self.assertEqual(item.strategy_scores, {"volume_breakout": 88.0})
        self.assertTrue(item.environment_ok)
        self.assertEqual(item.index_price, 3200.0)
        self.assertEqual(item.index_ma100, 3100.0)
        self.assertEqual(item.leader_score, 82.0)
        self.assertEqual(item.relative_strength_market, 1.2)
        self.assertEqual(item.relative_strength_sector, 1.4)
        self.assertEqual(item.ai_review["ai_trade_stage"], "focus")
        self.assertEqual(item.ai_trade_stage, "focus")
        self.assertEqual(item.ai_reasoning, "环境允许，等待确认。")
        self.assertEqual(item.ai_confidence, 0.81)
        self.assertTrue(item.ai_environment_ok)
        self.assertTrue(item.ai_theme_alignment)
        self.assertEqual(item.ai_entry_quality, "high")
        self.assertFalse(item.stage_conflict)

    def test_setup_type_none_serialized_as_null(self) -> None:
        """SetupType.NONE should become None in payload, not the string 'none'.

        Root cause: candidates without entry_core strategy match get
        SetupType.NONE, which _serialize_value converts to the string "none".
        The DB then stores "none" and the backtest aggregator treats it as a
        real strategy group.  The fix in to_payload() converts it back to None.
        """
        decision = CandidateDecision(
            code="000001",
            name="平安银行",
            setup_type=SetupType.NONE,
        )
        payload = decision.to_payload()
        self.assertIsNone(
            payload["setup_type"],
            "SetupType.NONE must be serialized as None, not 'none'",
        )

    def test_setup_type_real_value_preserved(self) -> None:
        """Real setup types must still serialize correctly."""
        decision = CandidateDecision(
            code="600519",
            name="贵州茅台",
            setup_type=SetupType.TREND_BREAKOUT,
        )
        payload = decision.to_payload()
        self.assertEqual(payload["setup_type"], "trend_breakout")

    def test_candidate_item_preserves_layered_divergence_v2_snapshot(self) -> None:
        """API schema 必须接受新 setup 字符串并原样保留嵌套 v2 证据。"""
        factor_snapshot = {
            "bottom_divergence_v2_stage": "near_cleared",
            "bottom_divergence_v2_candidate_version": "candidate-v2",
            "bottom_divergence_v2_zone_version": "zone-v2",
            "bottom_divergence_v2_candidate_records": [
                {
                    "candidate_version": "candidate-v2",
                    "zone": {
                        "zone_version": "zone-v2",
                        "r1": {
                            "lower": 37.46,
                            "upper": 39.2,
                            "touch_dates": [
                                "2026-07-02",
                                "2026-07-08",
                                "2026-07-10",
                            ],
                        },
                    },
                    "near_zone_events": {
                        "crossed": {"date": "2026-07-23"},
                        "cleared_confirmed": {"date": "2026-07-23"},
                    },
                }
            ],
        }

        item = ScreeningCandidateItem(
            code="001337",
            name="四川黄金",
            rank=1,
            rule_score=88.0,
            selected_for_ai=True,
            matched_strategies=["bottom_divergence_layered_entry_v2"],
            setup_type="bottom_divergence_layered_entry",
            factor_snapshot=factor_snapshot,
        )

        self.assertEqual(
            item.setup_type,
            "bottom_divergence_layered_entry",
        )
        self.assertEqual(item.factor_snapshot, factor_snapshot)

    def test_screening_run_response_accepts_theme_pipeline_snapshots(self) -> None:
        response = ScreeningRunResponse(
            run_id="run-theme-pipeline",
            status="completed",
            local_theme_pipeline={
                "source": "local",
                "selected_theme_names": ["AI芯片", "机器人概念"],
                "themes": [],
            },
            external_theme_pipeline={
                "source": "openclaw",
                "top_theme_names": ["AI芯片"],
                "themes": [],
            },
            fused_theme_pipeline={
                "active_sources": ["local", "external"],
                "selected_theme_names": ["AI芯片", "机器人概念"],
                "merged_theme_count": 2,
                "merged_themes": [],
            },
        )

        self.assertEqual(response.local_theme_pipeline.source, "local")
        self.assertEqual(response.external_theme_pipeline.source, "openclaw")
        self.assertEqual(response.fused_theme_pipeline.merged_theme_count, 2)


if __name__ == "__main__":
    unittest.main()
