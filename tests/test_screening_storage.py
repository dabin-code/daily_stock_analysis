import os
import tempfile
import time
import unittest
from datetime import date
from types import SimpleNamespace

from sqlalchemy import text

from src.config import Config
from src.search_service import SearchResponse
from src.search_service import SearchResult
from src.storage import DatabaseManager


class ScreeningStorageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test_screening_storage.db")
        os.environ["DATABASE_PATH"] = self._db_path

        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        self._temp_dir.cleanup()

    def test_create_run_update_status_and_save_candidates(self) -> None:
        run_id = self.db.create_screening_run(
            trade_date=date(2026, 3, 13),
            market="cn",
            config_snapshot={
                "candidate_limit": 30,
                "ai_top_k": 5,
            },
        )

        self.assertTrue(self.db.update_screening_run_status(run_id=run_id, status="resolving_universe"))
        self.assertTrue(self.db.update_screening_run_status(run_id=run_id, status="ingesting"))
        self.assertTrue(self.db.update_screening_run_status(run_id=run_id, status="factorizing"))
        self.assertTrue(self.db.update_screening_run_status(run_id=run_id, status="screening"))
        saved = self.db.save_screening_candidates(
            run_id=run_id,
            candidates=[
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "rank": 1,
                    "rule_score": 91.5,
                    "selected_for_ai": True,
                    "matched_strategies": ["volume_breakout", "dragon_head"],
                    "rule_hits": ["trend_aligned", "volume_expanding"],
                    "factor_snapshot": {"close": 1500.0, "ma20": 1480.0},
                    "ai_summary": "趋势完好，等待回踩确认。",
                    "ai_operation_advice": "关注",
                    "ai_trade_stage": "focus",
                    "result_source": "rules_plus_ai",
                    "ai_trade_stage": "focus",
                    "result_source": "rules_plus_ai",
                    "ai_trade_stage": "focus",
                    "result_source": "rules_plus_ai",
                    "ai_query_id": "query-1",
                },
                {
                    "code": "000001",
                    "name": "平安银行",
                    "rank": 2,
                    "rule_score": 82.0,
                    "selected_for_ai": False,
                    "rule_hits": ["trend_aligned"],
                    "factor_snapshot": {"close": 12.1, "ma20": 11.8},
                },
            ],
        )
        self.assertTrue(
            self.db.update_screening_run_status(
                run_id=run_id,
                status="completed",
                universe_size=120,
                candidate_count=2,
            )
        )

        self.assertEqual(saved, 2)

        run = self.db.get_screening_run(run_id)
        self.assertIsNotNone(run)
        self.assertEqual(run["mode"], "balanced")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["universe_size"], 120)
        self.assertEqual(run["candidate_count"], 2)

        candidates = self.db.list_screening_candidates(run_id)
        self.assertEqual([item["code"] for item in candidates], ["600519", "000001"])
        self.assertEqual(candidates[0]["matched_strategies"], ["volume_breakout", "dragon_head"])
        self.assertEqual(candidates[0]["rule_hits"], ["trend_aligned", "volume_expanding"])
        self.assertEqual(candidates[0]["factor_snapshot"]["ma20"], 1480.0)
        self.assertEqual(candidates[0]["ai_query_id"], "query-1")

    def test_raw_rule_score_is_persisted_and_read_back(self) -> None:
        """入库映射漏掉 raw_rule_score 时，单测会绿但库里全 NULL。

        五层优先级排序把 rule_score 原地覆盖成复合优先级分，
        raw_rule_score 是唯一还能取到原始质量分的地方；
        Rank IC 这类连续量分析全靠它。
        """
        run_id = self.db.create_screening_run(
            trade_date=date(2026, 3, 13),
            market="cn",
            config_snapshot={"candidate_limit": 30},
        )
        saved = self.db.save_screening_candidates(
            run_id=run_id,
            candidates=[
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "rank": 1,
                    "rule_score": 26.8,
                    "raw_rule_score": 80.0,
                    "rule_hits": ["trend_aligned"],
                    "factor_snapshot": {"close": 1500.0},
                },
                {
                    "code": "000001",
                    "name": "平安银行",
                    "rank": 2,
                    "rule_score": 15.2,
                    "rule_hits": [],
                    "factor_snapshot": {},
                },
            ],
        )
        self.assertEqual(saved, 2)

        # 必须直查列：to_dict 会把 candidate_decision_json 覆盖到行字段之上，
        # 而该 JSON 是整个入参的副本，读回来的值可能根本没经过这一列。
        # Rank IC 要的是可 SQL 聚合的列，不是 JSON blob 里的一个键。
        with self.db.get_session() as session:
            rows = session.execute(
                text(
                    "SELECT code, rule_score, raw_rule_score "
                    "FROM screening_candidates WHERE run_id = :run_id"
                ),
                {"run_id": run_id},
            ).all()
        by_code = {row[0]: row for row in rows}
        self.assertAlmostEqual(by_code["600519"][2], 80.0, places=4)
        # 复合优先级分必须原样保留，两个数不能被写成同一个。
        self.assertAlmostEqual(by_code["600519"][1], 26.8, places=4)
        # 上游没给原始分时不编造，留 NULL 让下游按「未知」剔除，而不是当 0 参与秩相关。
        self.assertIsNone(by_code["000001"][2])

    def test_find_latest_screening_run_ignores_runtime_ingest_context_in_identity(self) -> None:
        run_id = self.db.create_screening_run(
            trade_date=date(2026, 3, 13),
            market="cn",
            config_snapshot={
                "requested_trade_date": "2026-03-13",
                "mode": "balanced",
                "stock_codes": [],
                "candidate_limit": 30,
                "ai_top_k": 5,
                "screening_min_list_days": 120,
                "screening_min_volume_ratio": 1.2,
                "screening_breakout_lookback_days": 20,
                "screening_factor_lookback_days": 80,
                "screening_ingest_failure_threshold": 0.02,
            },
        )
        self.assertTrue(
            self.db.update_screening_run_context(
                run_id=run_id,
                config_snapshot_updates={
                    "failed_symbols": ["002859", "601555"],
                    "failed_symbol_reasons": {"002859": "empty_data", "601555": "fetch_failed"},
                    "warnings": ["已跳过同步失败股票: 002859, 601555"],
                    "sync_failure_ratio": 0.02,
                },
            )
        )

        matched = self.db.find_latest_screening_run(
            market="cn",
            config_snapshot={
                "requested_trade_date": "2026-03-13",
                "mode": "balanced",
                "stock_codes": [],
                "candidate_limit": 30,
                "ai_top_k": 5,
                "screening_min_list_days": 120,
                "screening_min_volume_ratio": 1.2,
                "screening_breakout_lookback_days": 20,
                "screening_factor_lookback_days": 80,
                "screening_ingest_failure_threshold": 0.02,
            },
        )

        self.assertIsNotNone(matched)
        self.assertEqual(matched["run_id"], run_id)

    def test_find_latest_screening_run_matches_legacy_snapshot_without_removed_avg_amount_key(self) -> None:
        run_id = self.db.create_screening_run(
            trade_date=date(2026, 3, 13),
            market="cn",
            config_snapshot={
                "requested_trade_date": "2026-03-13",
                "mode": "balanced",
                "stock_codes": [],
                "candidate_limit": 30,
                "ai_top_k": 5,
                "screening_min_list_days": 120,
                "screening_min_volume_ratio": 1.2,
                "screening_min_avg_amount": 50_000_000,
                "screening_breakout_lookback_days": 20,
                "screening_factor_lookback_days": 80,
                "screening_ingest_failure_threshold": 0.02,
            },
        )

        matched = self.db.find_latest_screening_run(
            market="cn",
            config_snapshot={
                "requested_trade_date": "2026-03-13",
                "mode": "balanced",
                "stock_codes": [],
                "candidate_limit": 30,
                "ai_top_k": 5,
                "screening_min_list_days": 120,
                "screening_min_volume_ratio": 1.2,
                "screening_breakout_lookback_days": 20,
                "screening_factor_lookback_days": 80,
                "screening_ingest_failure_threshold": 0.02,
            },
        )

        self.assertIsNotNone(matched)
        self.assertEqual(matched["run_id"], run_id)

    def test_list_screening_candidates_enriches_final_recommendation_fields(self) -> None:
        run_id = self.db.create_screening_run(
            trade_date=date(2026, 3, 13),
            market="cn",
            config_snapshot={"candidate_limit": 30, "ai_top_k": 5},
        )
        self.assertTrue(self.db.update_screening_run_status(run_id=run_id, status="screening"))
        self.db.save_screening_candidates(
            run_id=run_id,
            candidates=[
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "rank": 1,
                    "rule_score": 91.5,
                    "selected_for_ai": True,
                    "rule_hits": ["trend_aligned", "volume_expanding"],
                    "factor_snapshot": {"close": 1500.0},
                    "ai_summary": "趋势完好，等待回踩确认。",
                    "ai_operation_advice": "关注",
                    "ai_query_id": "query-1",
                },
                {
                    "code": "000001",
                    "name": "平安银行",
                    "rank": 2,
                    "rule_score": 82.0,
                    "selected_for_ai": False,
                    "rule_hits": ["trend_aligned"],
                    "factor_snapshot": {"close": 12.1},
                },
            ],
        )
        self.db.save_news_intel(
            code="600519",
            name="贵州茅台",
            dimension="latest_news",
            query="贵州茅台 600519 股票 最新消息",
            response=SearchResponse(
                query="贵州茅台 600519 股票 最新消息",
                provider="stub",
                success=True,
                results=[
                    SearchResult(
                        title="贵州茅台新品上市",
                        snippet="新品表现积极。",
                        url="https://example.com/news-1",
                        source="stub",
                    )
                ],
            ),
            query_context={"query_id": "query-1"},
        )

        items = self.db.list_screening_candidates(run_id)

        self.assertEqual(items[0]["recommendation_source"], "rules_only")
        self.assertEqual(items[0]["news_count"], 1)
        self.assertIn("贵州茅台新品上市", items[0]["news_summary"])
        self.assertIn("规则得分", items[0]["recommendation_reason"])
        self.assertNotIn("AI 二筛", items[0]["recommendation_reason"])
        self.assertEqual(items[0]["final_rank"], 1)
        self.assertEqual(items[1]["recommendation_source"], "rules_only")

    def test_list_screening_candidates_applies_limit_after_final_ranking(self) -> None:
        run_id = self.db.create_screening_run(
            trade_date=date(2026, 3, 13),
            market="cn",
            config_snapshot={"candidate_limit": 30, "ai_top_k": 5},
        )
        self.assertTrue(self.db.update_screening_run_status(run_id=run_id, status="screening"))
        self.db.save_screening_candidates(
            run_id=run_id,
            candidates=[
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "rank": 1,
                    "rule_score": 80.0,
                    "selected_for_ai": False,
                    "rule_hits": ["trend_aligned"],
                    "factor_snapshot": {},
                },
                {
                    "code": "000001",
                    "name": "平安银行",
                    "rank": 2,
                    "rule_score": 79.0,
                    "selected_for_ai": False,
                    "rule_hits": ["trend_aligned"],
                    "factor_snapshot": {},
                },
                {
                    "code": "300750",
                    "name": "宁德时代",
                    "rank": 3,
                    "rule_score": 78.0,
                    "selected_for_ai": True,
                    "rule_hits": ["trend_aligned"],
                    "factor_snapshot": {},
                    "ai_summary": "强趋势。",
                    "ai_operation_advice": "买入",
                    "ai_trade_stage": "probe_entry",
                    "result_source": "rules_plus_ai",
                    "ai_query_id": "query-300750",
                },
            ],
        )

        items = self.db.list_screening_candidates(run_id, limit=1)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["code"], "600519")
        self.assertEqual(items[0]["final_rank"], 1)

    def test_list_screening_candidates_does_not_mix_unbound_recent_news_into_snapshot(self) -> None:
        run_id = self.db.create_screening_run(
            trade_date=date(2026, 3, 13),
            market="cn",
            config_snapshot={"candidate_limit": 30, "ai_top_k": 0},
        )
        self.assertTrue(self.db.update_screening_run_status(run_id=run_id, status="screening"))
        self.db.save_screening_candidates(
            run_id=run_id,
            candidates=[
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "rank": 1,
                    "rule_score": 90.0,
                    "selected_for_ai": False,
                    "rule_hits": ["trend_aligned"],
                    "factor_snapshot": {},
                }
            ],
        )
        self.db.save_news_intel(
            code="600519",
            name="贵州茅台",
            dimension="latest_news",
            query="贵州茅台 600519 股票 最新消息",
            response=SearchResponse(
                query="贵州茅台 600519 股票 最新消息",
                provider="stub",
                success=True,
                results=[
                    SearchResult(
                        title="这是一条未绑定本次 run 的新闻",
                        snippet="不应混入候选快照。",
                        url="https://example.com/news-unbound",
                        source="stub",
                    )
                ],
            ),
        )

        items = self.db.list_screening_candidates(run_id)

        self.assertEqual(items[0]["news_count"], 0)
        self.assertIsNone(items[0]["news_summary"])

    def test_get_screening_candidate_detail_returns_analysis_history_reference(self) -> None:
        run_id = self.db.create_screening_run(
            trade_date=date(2026, 3, 13),
            market="cn",
            config_snapshot={"candidate_limit": 30, "ai_top_k": 5},
        )
        self.assertTrue(self.db.update_screening_run_status(run_id=run_id, status="screening"))
        self.db.save_screening_candidates(
            run_id=run_id,
            candidates=[
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "rank": 1,
                    "rule_score": 91.5,
                    "selected_for_ai": True,
                    "rule_hits": ["trend_aligned", "volume_expanding"],
                    "factor_snapshot": {"close": 1500.0},
                    "ai_summary": "趋势完好，等待回踩确认。",
                    "ai_operation_advice": "关注",
                    "ai_query_id": "query-detail-1",
                }
            ],
        )
        self.db.save_analysis_history(
            result=SimpleNamespace(
                code="600519",
                name="贵州茅台",
                sentiment_score=78,
                trend_prediction="看多",
                operation_advice="关注",
                analysis_summary="AI 深析认为趋势未破坏。",
                dashboard=None,
            ),
            query_id="query-detail-1",
            report_type="simple",
            news_content="新闻摘要",
            context_snapshot=None,
            save_snapshot=False,
        )

        item = self.db.get_screening_candidate_detail(run_id=run_id, code="600519")

        self.assertIsNotNone(item)
        self.assertEqual(item["code"], "600519")
        self.assertEqual(item["analysis_history"]["query_id"], "query-detail-1")
        self.assertEqual(item["analysis_history"]["stock_code"], "600519")
        self.assertEqual(item["analysis_history"]["analysis_summary"], "AI 深析认为趋势未破坏。")

    def test_get_screening_candidate_detail_keeps_global_final_rank(self) -> None:
        run_id = self.db.create_screening_run(
            trade_date=date(2026, 3, 13),
            market="cn",
            config_snapshot={"candidate_limit": 30, "ai_top_k": 5},
        )
        self.assertTrue(self.db.update_screening_run_status(run_id=run_id, status="screening"))
        self.db.save_screening_candidates(
            run_id=run_id,
            candidates=[
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "rank": 1,
                    "rule_score": 80.0,
                    "selected_for_ai": False,
                    "rule_hits": ["trend_aligned"],
                    "factor_snapshot": {},
                },
                {
                    "code": "300750",
                    "name": "宁德时代",
                    "rank": 2,
                    "rule_score": 79.0,
                    "selected_for_ai": True,
                    "rule_hits": ["trend_aligned"],
                    "factor_snapshot": {},
                    "ai_summary": "强趋势。",
                    "ai_operation_advice": "买入",
                    "ai_trade_stage": "probe_entry",
                    "result_source": "rules_plus_ai",
                    "ai_query_id": "query-rank-1",
                },
            ],
        )

        item = self.db.get_screening_candidate_detail(run_id=run_id, code="600519")

        self.assertIsNotNone(item)
        self.assertEqual(item["final_rank"], 1)

    def test_list_screening_candidates_keeps_rule_order_when_ai_review_exists(self) -> None:
        run_id = self.db.create_screening_run(
            trade_date=date(2026, 3, 13),
            market="cn",
            config_snapshot={"candidate_limit": 30, "ai_top_k": 5},
        )
        self.assertTrue(self.db.update_screening_run_status(run_id=run_id, status="screening"))
        self.db.save_screening_candidates(
            run_id=run_id,
            candidates=[
                {
                    "code": "600519",
                    "name": "璐靛窞鑼呭彴",
                    "rank": 1,
                    "rule_score": 80.0,
                    "selected_for_ai": False,
                    "rule_hits": ["trend_aligned"],
                    "factor_snapshot": {},
                },
                {
                    "code": "300750",
                    "name": "瀹佸痉鏃朵唬",
                    "rank": 2,
                    "rule_score": 79.0,
                    "selected_for_ai": True,
                    "rule_hits": ["trend_aligned"],
                    "factor_snapshot": {},
                    "ai_summary": "寮鸿秼鍔裤€?",
                    "ai_operation_advice": "涔板叆",
                    "ai_trade_stage": "probe_entry",
                    "result_source": "rules_plus_ai",
                    "ai_query_id": "query-order-1",
                },
            ],
        )
        self.db.save_news_intel(
            code="300750",
            name="瀹佸痉鏃朵唬",
            dimension="latest_news",
            query="瀹佸痉鏃朵唬 300750 鑲＄エ 鏈€鏂版秷鎭?",
            response=SearchResponse(
                query="瀹佸痉鏃朵唬 300750 鑲＄エ 鏈€鏂版秷鎭?",
                provider="stub",
                success=True,
                results=[
                    SearchResult(
                        title="瀹佸痉鏃朵唬鏂伴椈 1",
                        snippet="杩欐槸涓€鏉℃柊闂汇€?",
                        url="https://example.com/news-order-1",
                        source="stub",
                    ),
                    SearchResult(
                        title="瀹佸痉鏃朵唬鏂伴椈 2",
                        snippet="杩欐槸绗簩鏉℃柊闂汇€?",
                        url="https://example.com/news-order-2",
                        source="stub",
                    ),
                ],
            ),
            query_context={"query_id": "query-order-1"},
        )

        items = self.db.list_screening_candidates(run_id)

        self.assertEqual([item["code"] for item in items], ["600519", "300750"])
        self.assertEqual(items[0]["final_rank"], 1)
        self.assertEqual(items[1]["final_rank"], 2)
        self.assertEqual(items[0]["final_score"], 80.0)
        self.assertEqual(items[1]["final_score"], 79.0)
        self.assertEqual(items[1]["recommendation_source"], "rules_plus_ai")
        self.assertEqual(items[1]["news_count"], 2)

    def test_get_screening_candidate_detail_does_not_link_history_from_other_code_with_same_query_id(self) -> None:
        run_id = self.db.create_screening_run(
            trade_date=date(2026, 3, 13),
            market="cn",
            config_snapshot={"candidate_limit": 30, "ai_top_k": 5},
        )
        self.assertTrue(self.db.update_screening_run_status(run_id=run_id, status="screening"))
        self.db.save_screening_candidates(
            run_id=run_id,
            candidates=[
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "rank": 1,
                    "rule_score": 91.5,
                    "selected_for_ai": True,
                    "rule_hits": ["trend_aligned"],
                    "factor_snapshot": {"close": 1500.0},
                    "ai_summary": "趋势完好，等待回踩确认。",
                    "ai_operation_advice": "关注",
                    "ai_query_id": "query-duplicate",
                }
            ],
        )
        self.db.save_analysis_history(
            result=SimpleNamespace(
                code="000001",
                name="平安银行",
                sentiment_score=60,
                trend_prediction="震荡",
                operation_advice="观望",
                analysis_summary="这条历史不应被错误关联。",
                dashboard=None,
            ),
            query_id="query-duplicate",
            report_type="simple",
            news_content="新闻摘要",
            context_snapshot=None,
            save_snapshot=False,
        )

        item = self.db.get_screening_candidate_detail(run_id=run_id, code="600519")

        self.assertIsNotNone(item)
        self.assertIsNone(item["analysis_history"])

    def test_update_screening_run_status_rejects_invalid_transition(self) -> None:
        run_id = self.db.create_screening_run(
            trade_date=date(2026, 3, 13),
            market="cn",
            config_snapshot={},
        )

        self.assertTrue(self.db.update_screening_run_status(run_id=run_id, status="resolving_universe"))
        self.assertTrue(self.db.update_screening_run_status(run_id=run_id, status="ingesting"))
        self.assertTrue(self.db.update_screening_run_status(run_id=run_id, status="factorizing"))
        self.assertTrue(self.db.update_screening_run_status(run_id=run_id, status="screening"))
        completed = self.db.update_screening_run_status(run_id=run_id, status="completed")
        rolled_back = self.db.update_screening_run_status(run_id=run_id, status="screening")

        self.assertTrue(completed)
        self.assertFalse(rolled_back)

        run = self.db.get_screening_run(run_id)
        self.assertIsNotNone(run)
        self.assertEqual(run["status"], "completed")
        self.assertIsNotNone(run["completed_at"])

    def test_create_screening_run_detects_duplicate_run_id_conflict(self) -> None:
        run_id, created = self.db.create_screening_run(
            trade_date=date(2026, 3, 13),
            market="cn",
            config_snapshot={"mode": "balanced"},
            run_id="run-fixed-id",
            return_created=True,
        )
        duplicate_run_id, duplicate_created = self.db.create_screening_run(
            trade_date=date(2026, 3, 13),
            market="cn",
            config_snapshot={"mode": "balanced"},
            run_id="run-fixed-id",
            return_created=True,
        )

        self.assertEqual(run_id, "run-fixed-id")
        self.assertTrue(created)
        self.assertEqual(duplicate_run_id, "run-fixed-id")
        self.assertFalse(duplicate_created)

    def test_update_screening_run_status_keeps_completed_at_for_same_terminal_status(self) -> None:
        run_id = self.db.create_screening_run(
            trade_date=date(2026, 3, 13),
            market="cn",
            config_snapshot={},
        )

        self.assertTrue(self.db.update_screening_run_status(run_id=run_id, status="resolving_universe"))
        self.assertTrue(self.db.update_screening_run_status(run_id=run_id, status="ingesting"))
        self.assertTrue(self.db.update_screening_run_status(run_id=run_id, status="factorizing"))
        self.assertTrue(self.db.update_screening_run_status(run_id=run_id, status="screening"))
        self.assertTrue(self.db.update_screening_run_status(run_id=run_id, status="completed"))
        first_run = self.db.get_screening_run(run_id)
        time.sleep(0.01)
        self.assertTrue(
            self.db.update_screening_run_status(
                run_id=run_id,
                status="completed",
                candidate_count=1,
            )
        )
        second_run = self.db.get_screening_run(run_id)

        self.assertIsNotNone(first_run)
        self.assertIsNotNone(second_run)
        self.assertEqual(first_run["completed_at"], second_run["completed_at"])
        self.assertEqual(second_run["candidate_count"], 1)

    def test_save_screening_candidates_rejects_missing_run(self) -> None:
        with self.assertRaises(Exception):
            self.db.save_screening_candidates(
                run_id="missing-run",
                candidates=[
                    {
                        "code": "600519",
                        "name": "贵州茅台",
                        "rank": 1,
                        "rule_score": 91.5,
                    }
                ],
            )

    def test_pending_run_cannot_jump_directly_to_completed(self) -> None:
        run_id = self.db.create_screening_run(
            trade_date=date(2026, 3, 13),
            market="cn",
            config_snapshot={},
        )

        updated = self.db.update_screening_run_status(run_id=run_id, status="completed")

        self.assertFalse(updated)
        run = self.db.get_screening_run(run_id)
        self.assertIsNotNone(run)
        self.assertEqual(run["status"], "pending")


if __name__ == "__main__":
    unittest.main()
