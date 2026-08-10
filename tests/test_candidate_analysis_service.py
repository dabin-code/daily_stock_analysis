import json
import unittest
from unittest.mock import MagicMock

from src.schemas.trading_types import SetupType, TradeStage
from src.search_service import SearchResponse
from src.search_service import SearchResult
from src.services.candidate_analysis_service import CandidateAnalysisBatchResult
from src.services.candidate_analysis_service import CandidateAnalysisService
from src.services.screening_ai_review_service import ScreeningAiReviewService


def _mock_review(query_id: str = "query-600519") -> MagicMock:
    review = MagicMock()
    review.to_payload.return_value = {
        "result_source": "rules_plus_ai",
        "fallback_reason": None,
    }
    review.reasoning_summary = "趋势延续。"
    review.ai_operation_advice = "focus"
    review.ai_query_id = query_id
    review.result_source = "rules_plus_ai"
    review.fallback_reason = None
    review.trade_stage = "focus"
    review.confidence = 0.75
    review.environment_ok = True
    review.initial_position = "1/4"
    review.stop_loss_rule = "跌破MA20离场"
    review.take_profit_plan = "沿5日线止盈"
    review.invalidation_rule = "放量长阴失效"
    return review


class _CapturingLlm:
    model_name = "fake-local"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate_text(
        self,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float,
    ) -> str:
        self.prompts.append(prompt)
        return json.dumps(
            {
                "environment_ok": True,
                "trade_stage": "add_on_strength",
                "entry_maturity": "high",
                "setup_type": "bottom_divergence_layered_entry",
                "risk_level": "low",
                "initial_position": "1/2",
                "stop_loss_rule": "跌破当前层级止损位",
                "take_profit_plan": "沿趋势止盈",
                "invalidation_rule": "跌破结构失效",
                "reasoning_summary": "历史突破，尝试升级",
                "confidence": 0.92,
            },
            ensure_ascii=False,
        )


def _prompt_input(prompt: str) -> dict:
    return json.loads(
        prompt.split("Input:\n", 1)[1].split("\nOutput schema:", 1)[0]
    )


class CandidateAnalysisServiceTestCase(unittest.TestCase):
    def test_analyze_top_k_searches_news_for_top_m_without_blocking_ai(self) -> None:
        screening_ai_review_service = MagicMock()
        screening_ai_review_service.review_candidate.return_value = _mock_review()

        search_service = MagicMock()
        search_service.is_available = True
        search_service.search_stock_news.side_effect = [
            SearchResponse(
                query="贵州茅台 600519 股票 最新消息",
                provider="stub",
                success=True,
                results=[
                    SearchResult(
                        title="贵州茅台发布新品",
                        snippet="新品反馈积极。",
                        url="https://example.com/1",
                        source="stub",
                    )
                ],
            ),
            RuntimeError("news failed"),
        ]

        db = MagicMock()
        service = CandidateAnalysisService(
            search_service=search_service,
            db_manager=db,
            screening_ai_review_service=screening_ai_review_service,
        )

        batch = service.analyze_top_k(
            candidates=[
                {"code": "600519", "name": "贵州茅台", "rank": 1},
                {"code": "000001", "name": "平安银行", "rank": 2},
            ],
            top_k=1,
            news_top_m=2,
        )

        self.assertIsInstance(batch, CandidateAnalysisBatchResult)
        self.assertIn("600519", batch.results)
        self.assertEqual(batch.results["600519"]["ai_query_id"], "query-600519")
        self.assertEqual(search_service.search_stock_news.call_count, 2)
        db.save_news_intel.assert_called_once()

    def test_analyze_top_k_reports_failed_codes_when_single_ai_call_fails(self) -> None:
        screening_ai_review_service = MagicMock()
        screening_ai_review_service.review_candidate.side_effect = [
            _mock_review(),
            RuntimeError("ai timeout"),
        ]

        search_service = MagicMock()
        search_service.is_available = False

        service = CandidateAnalysisService(
            search_service=search_service,
            db_manager=MagicMock(),
            screening_ai_review_service=screening_ai_review_service,
        )

        batch = service.analyze_top_k(
            candidates=[
                {"code": "600519", "name": "贵州茅台", "rank": 1},
                {"code": "000001", "name": "平安银行", "rank": 2},
            ],
            top_k=2,
        )

        self.assertEqual(batch.failed_codes, ["000001"])

    def test_analyze_top_k_binds_news_only_candidate_to_stable_query_id(self) -> None:
        screening_ai_review_service = MagicMock()
        screening_ai_review_service.review_candidate.return_value = _mock_review()

        search_service = MagicMock()
        search_service.is_available = True
        search_service.search_stock_news.side_effect = [
            SearchResponse(
                query="贵州茅台 600519 股票 最新消息",
                provider="stub",
                success=True,
                results=[
                    SearchResult(
                        title="贵州茅台发布新品",
                        snippet="新品反馈积极。",
                        url="https://example.com/1",
                        source="stub",
                    )
                ],
            ),
            SearchResponse(
                query="平安银行 000001 股票 最新消息",
                provider="stub",
                success=True,
                results=[
                    SearchResult(
                        title="平安银行业绩预告",
                        snippet="盈利改善。",
                        url="https://example.com/2",
                        source="stub",
                    )
                ],
            ),
        ]

        db = MagicMock()
        service = CandidateAnalysisService(
            search_service=search_service,
            db_manager=db,
            screening_ai_review_service=screening_ai_review_service,
        )

        batch = service.analyze_top_k(
            candidates=[
                {"code": "600519", "name": "贵州茅台", "rank": 1},
                {"code": "000001", "name": "平安银行", "rank": 2},
            ],
            top_k=1,
            news_top_m=2,
        )

        self.assertIn("000001", batch.results)
        self.assertTrue(str(batch.results["000001"]["ai_query_id"]).startswith("screening-news-"))

    def test_real_review_path_carries_v2_evidence_and_blocks_stale_upgrade(
        self,
    ) -> None:
        llm_client = _CapturingLlm()
        review_service = ScreeningAiReviewService(llm_client=llm_client)
        search_service = MagicMock()
        search_service.is_available = False
        service = CandidateAnalysisService(
            analysis_service=MagicMock(),
            search_service=search_service,
            db_manager=MagicMock(),
            screening_ai_review_service=review_service,
        )
        factor_snapshot = {
            "bottom_divergence_v2_stage": "stale",
            "bottom_divergence_v2_pattern_code": "macd_price",
            "bottom_divergence_v2_early_strength": 0.81,
            "bottom_divergence_v2_near_zone_lower": 24.1,
            "bottom_divergence_v2_near_zone_upper": 24.8,
            "bottom_divergence_v2_near_zone_score": 0.71,
            "bottom_divergence_v2_major_zone_lower": 27.2,
            "bottom_divergence_v2_major_zone_upper": 27.8,
            "bottom_divergence_v2_major_zone_score": 0.92,
            "bottom_divergence_v2_candidate_version": "primary",
            "bottom_divergence_v2_zone_version": "causal_v2",
            "bottom_divergence_v2_degradation_reasons": ["stale"],
            "bottom_divergence_v2_major_breakout": True,
            "bottom_divergence_v2_major_actionable_entry": False,
            "bottom_divergence_v2_actionability_status": "confirmation_too_old",
            "bottom_divergence_v2_event_days": 8,
            "bottom_divergence_v2_stop_loss_price": 23.5,
            "bottom_divergence_v2_layered_buy_points": [
                {
                    "level": "r2",
                    "price": 27.8,
                    "stop": 23.5,
                    "triggered": True,
                }
            ],
        }

        batch = service.analyze_top_k(
            candidates=[
                {
                    "code": "001337",
                    "name": "四川黄金",
                    "setup_type": SetupType.BOTTOM_DIVERGENCE_LAYERED_ENTRY.value,
                    "trade_stage": TradeStage.ADD_ON_STRENGTH.value,
                    "factor_snapshot": factor_snapshot,
                }
            ],
            top_k=1,
            news_top_m=0,
        )

        self.assertEqual(len(llm_client.prompts), 1)
        evidence = _prompt_input(llm_client.prompts[0])["setup"][
            "bottom_divergence_v2_evidence"
        ]
        self.assertEqual(evidence["stage"], "stale")
        self.assertEqual(
            evidence["r2"],
            {
                "lower": 27.2,
                "upper": 27.8,
                "score": 0.92,
                "touch_dates": [],
            },
        )
        self.assertTrue(evidence["major_breakout"])
        self.assertFalse(evidence["major_actionable_entry"])
        result = batch.results["001337"]
        self.assertEqual(result["prompt_version"], "screening_ai_review_v2")
        self.assertEqual(result["ai_trade_stage"], TradeStage.FOCUS.value)
        self.assertEqual(result["ai_operation_advice"], TradeStage.FOCUS.value)
        self.assertIsNone(result["initial_position"])
        self.assertIn(
            "v2_non_actionable_stage",
            result["downgrade_reasons"],
        )


if __name__ == "__main__":
    unittest.main()
