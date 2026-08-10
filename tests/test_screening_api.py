import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import src.auth as auth
from api.app import create_app
from src.config import Config
from src.services.screening_task_service import ScreeningTradeDateNotReadyError


class ScreeningApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_path = Path(self.temp_dir.name) / ".env"
        self.env_path.write_text(
            "\n".join(
                [
                    "STOCK_LIST=600519,000001",
                    "ADMIN_AUTH_ENABLED=false",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.environ["ENV_FILE"] = str(self.env_path)
        Config.reset_instance()

        auth._auth_enabled = None
        self.auth_patcher = patch.object(auth, "_is_auth_enabled_from_env", return_value=False)
        self.auth_patcher.start()

        app = create_app(static_dir=Path(self.temp_dir.name) / "empty-static")
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.auth_patcher.stop()
        Config.reset_instance()
        os.environ.pop("ENV_FILE", None)
        self.temp_dir.cleanup()

    @patch("api.v1.endpoints.screening.ScreeningTaskService")
    def test_create_run_and_query_results(self, service_cls) -> None:
        service = service_cls.return_value
        service.config.screening_default_mode = "balanced"
        service.config.screening_candidate_limit = 30
        service.config.screening_ai_top_k = 5
        service.resolve_run_config.return_value.candidate_limit = 30
        service.resolve_run_config.return_value.ai_top_k = 5
        service.execute_run.return_value = {
            "run_id": "run-20260313-1",
            "mode": "balanced",
            "status": "completed",
            "candidate_count": 2,
            "universe_size": 120,
        }
        service.list_runs.return_value = [
            {
                "run_id": "run-20260313-1",
                "mode": "balanced",
                "status": "completed",
                "candidate_count": 2,
                "universe_size": 120,
            }
        ]
        service.get_run.return_value = {
            "run_id": "run-20260313-1",
            "mode": "balanced",
            "status": "completed",
            "candidate_count": 2,
            "universe_size": 120,
        }
        service.list_candidates.return_value = [
            {
                "code": "600519",
                "name": "贵州茅台",
                "rank": 1,
                "rule_score": 90.5,
                "selected_for_ai": True,
                "matched_strategies": ["volume_breakout"],
                "rule_hits": ["trend_aligned"],
                "factor_snapshot": {"close": 1500.0},
                "ai_summary": "趋势良好",
                "ai_operation_advice": "关注",
            }
        ]

        create_resp = self.client.post(
            "/api/v1/screening/runs",
            json={
                "trade_date": "2026-03-13",
                "stock_codes": ["600519", "000001"],
                "candidate_limit": 30,
                "ai_top_k": 5,
            },
        )
        self.assertEqual(create_resp.status_code, 200)
        self.assertEqual(create_resp.json()["run_id"], "run-20260313-1")

        list_resp = self.client.get("/api/v1/screening/runs")
        self.assertEqual(list_resp.status_code, 200)
        self.assertEqual(list_resp.json()["total"], 1)

        detail_resp = self.client.get("/api/v1/screening/runs/run-20260313-1")
        self.assertEqual(detail_resp.status_code, 200)
        self.assertEqual(detail_resp.json()["status"], "completed")

        candidate_resp = self.client.get("/api/v1/screening/runs/run-20260313-1/candidates")
        self.assertEqual(candidate_resp.status_code, 200)
        self.assertEqual(candidate_resp.json()["items"][0]["code"], "600519")
        self.assertEqual(candidate_resp.json()["items"][0]["matched_strategies"], ["volume_breakout"])

    @patch("api.v1.endpoints.screening.ScreeningTaskService")
    def test_create_run_returns_failed_resource_payload(self, service_cls) -> None:
        service = service_cls.return_value
        service.config.screening_default_mode = "balanced"
        service.config.screening_candidate_limit = 30
        service.config.screening_ai_top_k = 5
        service.resolve_run_config.return_value.candidate_limit = 30
        service.resolve_run_config.return_value.ai_top_k = 5
        service.execute_run.return_value = {
            "run_id": "run-20260313-2",
            "mode": "balanced",
            "status": "failed",
            "error_summary": "mocked failure",
        }

        response = self.client.post(
            "/api/v1/screening/runs",
            json={
                "trade_date": "2026-03-13",
                "stock_codes": ["600519"],
                "market": "cn",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["run_id"], "run-20260313-2")
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error_summary"], "mocked failure")

    @patch("api.v1.endpoints.screening.ScreeningTaskService")
    def test_create_run_returns_ai_degraded_resource_payload(self, service_cls) -> None:
        service = service_cls.return_value
        service.config.screening_default_mode = "balanced"
        service.config.screening_candidate_limit = 30
        service.config.screening_ai_top_k = 5
        service.resolve_run_config.return_value.candidate_limit = 30
        service.resolve_run_config.return_value.ai_top_k = 5
        service.execute_run.return_value = {
            "run_id": "run-20260313-ai-degraded",
            "mode": "balanced",
            "status": "completed_with_ai_degraded",
            "candidate_count": 2,
            "error_summary": "ai timeout",
        }

        response = self.client.post(
            "/api/v1/screening/runs",
            json={
                "trade_date": "2026-03-13",
                "stock_codes": ["600519"],
                "market": "cn",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "completed_with_ai_degraded")

    @patch("src.agent.skills.base.SkillManager")
    @patch("api.v1.endpoints.screening.ScreeningTaskService")
    def test_openclaw_theme_run_uses_skill_manager_and_requested_trade_date(
        self,
        service_cls,
        skill_manager_cls,
    ) -> None:
        service = service_cls.return_value
        service.execute_run.return_value = {
            "run_id": "run-openclaw-1",
            "status": "completed",
        }
        skill_manager = skill_manager_cls.return_value

        response = self.client.post(
            "/api/v1/screening/openclaw-theme-run",
            json={
                "trade_date": "2026-03-27",
                "market": "cn",
                "themes": [
                    {
                        "name": "AI芯片",
                        "heat_score": 90,
                        "confidence": 0.9,
                        "catalyst_summary": "政策催化",
                        "keywords": ["AI", "芯片", "算力"],
                        "evidence": [],
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        skill_manager.load_builtin_strategies.assert_called_once()
        service_cls.assert_called_once_with(skill_manager=skill_manager)
        self.assertEqual(service.execute_run.call_args.kwargs["trade_date"], date(2026, 3, 27))
        self.assertEqual(service.execute_run.call_args.kwargs["strategy_names"], ["extreme_strength_combo"])
        self.assertEqual(service.execute_run.call_args.kwargs["theme_context"].trade_date, "2026-03-27")
        self.assertEqual(service.execute_run.call_args.kwargs["theme_context"].themes[0].name, "AI芯片")
        service._update_run_context.assert_not_called()

    def test_openclaw_theme_run_rejects_ai_top_k_greater_than_candidate_limit(self) -> None:
        response = self.client.post(
            "/api/v1/screening/openclaw-theme-run",
            json={
                "trade_date": "2026-03-27",
                "market": "cn",
                "themes": [
                    {
                        "name": "AI鑺墖",
                        "heat_score": 90,
                        "confidence": 0.9,
                        "catalyst_summary": "鏀跨瓥鍌寲",
                        "keywords": ["AI", "鑺墖", "绠楀姏"],
                        "evidence": [],
                    }
                ],
                "options": {
                    "candidate_limit": 5,
                    "ai_top_k": 10,
                },
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"], "validation_error")
        self.assertEqual(
            response.json()["message"],
            "ai_top_k cannot be greater than candidate_limit",
        )

    @patch("api.v1.endpoints.screening.ScreeningTaskService")
    def test_create_run_returns_failed_symbols_and_warnings(self, service_cls) -> None:
        service = service_cls.return_value
        service.config.screening_default_mode = "balanced"
        service.config.screening_candidate_limit = 30
        service.config.screening_ai_top_k = 5
        service.resolve_run_config.return_value.candidate_limit = 30
        service.resolve_run_config.return_value.ai_top_k = 5
        service.execute_run.return_value = {
            "run_id": "run-20260313-warnings",
            "mode": "balanced",
            "status": "completed",
            "candidate_count": 1,
            "failed_symbols": ["002859", "601555"],
            "warnings": ["已跳过同步失败股票: 002859, 601555"],
            "sync_failure_ratio": 0.02,
        }

        response = self.client.post(
            "/api/v1/screening/runs",
            json={
                "trade_date": "2026-03-13",
                "stock_codes": ["600519"],
                "market": "cn",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["failed_symbols"], ["002859", "601555"])
        self.assertEqual(payload["warnings"], ["已跳过同步失败股票: 002859, 601555"])
        self.assertEqual(payload["sync_failure_ratio"], 0.02)

    @patch("api.v1.endpoints.screening.ScreeningTaskService")
    def test_create_run_uses_config_defaults_when_limits_omitted(self, service_cls) -> None:
        service = service_cls.return_value
        service.config.screening_default_mode = "balanced"
        service.config.screening_candidate_limit = 30
        service.config.screening_ai_top_k = 5
        service.resolve_run_config.return_value.candidate_limit = 30
        service.resolve_run_config.return_value.ai_top_k = 5
        service.execute_run.return_value = {
            "run_id": "run-20260313-3",
            "status": "completed",
            "candidate_count": 1,
            "universe_size": 10,
        }

        response = self.client.post(
            "/api/v1/screening/runs",
            json={
                "trade_date": "2026-03-13",
                "stock_codes": ["600519"],
                "market": "cn",
            },
        )

        self.assertEqual(response.status_code, 200)
        service.execute_run.assert_called_once()
        self.assertIsNone(service.execute_run.call_args.kwargs["mode"])
        self.assertIsNone(service.execute_run.call_args.kwargs["candidate_limit"])
        self.assertIsNone(service.execute_run.call_args.kwargs["ai_top_k"])

    @patch("api.v1.endpoints.screening.ScreeningTaskService")
    def test_candidates_returns_404_when_run_missing(self, service_cls) -> None:
        service = service_cls.return_value
        service.get_run.return_value = None

        response = self.client.get("/api/v1/screening/runs/missing-run/candidates")
        self.assertEqual(response.status_code, 404)

    @patch("api.v1.endpoints.screening.ScreeningTaskService")
    def test_candidates_returns_409_when_run_not_completed(self, service_cls) -> None:
        service = service_cls.return_value
        service.get_run.return_value = {
            "run_id": "run-failed",
            "mode": "balanced",
            "status": "failed",
            "candidate_count": 0,
            "universe_size": 0,
        }

        response = self.client.get("/api/v1/screening/runs/run-failed/candidates")
        self.assertEqual(response.status_code, 409)

    @patch("api.v1.endpoints.screening.ScreeningTaskService")
    def test_candidates_returns_200_for_ai_degraded_run(self, service_cls) -> None:
        service = service_cls.return_value
        service.get_run.return_value = {
            "run_id": "run-ai-degraded",
            "mode": "balanced",
            "status": "completed_with_ai_degraded",
            "candidate_count": 1,
            "universe_size": 10,
        }
        service.list_candidates.return_value = [
            {
                "code": "600519",
                "name": "贵州茅台",
                "rank": 1,
                "rule_score": 90.0,
                "selected_for_ai": True,
                "rule_hits": ["trend_aligned"],
                "factor_snapshot": {"close": 1500.0},
                "recommendation_source": "rules_plus_ai",
                "final_score": 94.0,
                "final_rank": 1,
            }
        ]

        response = self.client.get("/api/v1/screening/runs/run-ai-degraded/candidates")
        self.assertEqual(response.status_code, 200)

    @patch("api.v1.endpoints.screening.ScreeningTaskService")
    def test_candidate_detail_returns_linked_analysis_history(self, service_cls) -> None:
        service = service_cls.return_value
        service.get_run.return_value = {
            "run_id": "run-20260313-1",
            "mode": "balanced",
            "status": "completed",
            "candidate_count": 1,
            "universe_size": 120,
        }
        service.get_candidate_detail.return_value = {
            "code": "600519",
            "name": "贵州茅台",
            "rank": 1,
            "rule_score": 90.5,
            "selected_for_ai": True,
            "matched_strategies": ["volume_breakout"],
            "rule_hits": ["trend_aligned"],
            "factor_snapshot": {"close": 1500.0},
            "ai_query_id": "query-detail-1",
            "ai_summary": "趋势良好",
            "ai_operation_advice": "关注",
            "analysis_history": {
                "id": 101,
                "query_id": "query-detail-1",
                "stock_code": "600519",
                "stock_name": "贵州茅台",
                "report_type": "simple",
                "operation_advice": "关注",
                "trend_prediction": "看多",
                "sentiment_score": 78,
                "analysis_summary": "AI 深析认为趋势未破坏。",
                "created_at": "2026-03-13T15:00:00",
            },
        }

        response = self.client.get("/api/v1/screening/runs/run-20260313-1/candidates/600519")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["code"], "600519")
        self.assertEqual(payload["matched_strategies"], ["volume_breakout"])
        self.assertEqual(payload["analysis_history"]["query_id"], "query-detail-1")

    @patch("api.v1.endpoints.screening.ScreeningTaskService")
    def test_layered_divergence_list_omits_records_but_detail_keeps_them(
        self,
        service_cls,
    ) -> None:
        service = service_cls.return_value
        service.get_run.return_value = {
            "run_id": "run-layered-divergence-v2",
            "mode": "balanced",
            "status": "completed",
            "candidate_count": 1,
            "universe_size": 1,
        }
        factor_snapshot = {
            "bottom_divergence_v2_stage": "major_actionable",
            "bottom_divergence_v2_candidate_version": "candidate-v2",
            "bottom_divergence_v2_zone_version": "zone-v2",
            "bottom_divergence_v2_candidate_records": [
                {
                    "candidate_version": "candidate-v2",
                    "zone": {
                        "zone_version": "zone-v2",
                        "r1": {"lower": 37.46, "upper": 39.2},
                        "r2": {"lower": 40.61, "upper": 42.9},
                    },
                    "major_zone_breakout": {
                        "confirmed": True,
                        "date": "2026-08-05",
                    },
                }
            ],
        }
        candidate = {
            "code": "001337",
            "name": "四川黄金",
            "rank": 1,
            "rule_score": 95.0,
            "selected_for_ai": True,
            "matched_strategies": [
                "bottom_divergence_layered_entry_v2"
            ],
            "rule_hits": [
                "strategy:bottom_divergence_layered_entry_v2"
            ],
            "setup_type": "bottom_divergence_layered_entry",
            "factor_snapshot": factor_snapshot,
        }
        service.list_candidates.return_value = [candidate]
        service.get_candidate_detail.return_value = candidate

        list_response = self.client.get(
            "/api/v1/screening/runs/run-layered-divergence-v2/candidates"
        )
        detail_response = self.client.get(
            "/api/v1/screening/runs/run-layered-divergence-v2/candidates/001337"
        )

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(detail_response.status_code, 200)
        list_item = list_response.json()["items"][0]
        detail_item = detail_response.json()
        self.assertEqual(
            list_item["setup_type"],
            "bottom_divergence_layered_entry",
        )
        self.assertNotIn(
            "bottom_divergence_v2_candidate_records",
            list_item["factor_snapshot"],
        )
        self.assertEqual(detail_item["factor_snapshot"], factor_snapshot)
        self.assertIn(
            "bottom_divergence_v2_candidate_records",
            candidate["factor_snapshot"],
        )
        self.assertIs(candidate["factor_snapshot"], factor_snapshot)

    @patch("api.v1.endpoints.screening.ScreeningTaskService")
    def test_candidate_list_sanitizes_one_hundred_items_without_mutation(
        self,
        service_cls,
    ) -> None:
        service = service_cls.return_value
        service.get_run.return_value = {
            "run_id": "run-list-100",
            "mode": "balanced",
            "status": "completed",
            "candidate_count": 100,
            "universe_size": 100,
        }
        items = [
            {
                "code": f"{index:06d}",
                "name": f"candidate-{index}",
                "rank": index + 1,
                "rule_score": 80.0,
                "selected_for_ai": False,
                "factor_snapshot": {
                    "close": float(index + 1),
                    "bottom_divergence_v2_candidate_records": [
                        {"unique_marker": f"record-{index}"}
                    ],
                },
            }
            for index in range(100)
        ]
        service.list_candidates.return_value = items

        response = self.client.get(
            "/api/v1/screening/runs/run-list-100/candidates?limit=100"
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["total"], 100)
        self.assertEqual(len(payload["items"]), 100)
        self.assertTrue(all(
            "bottom_divergence_v2_candidate_records"
            not in item["factor_snapshot"]
            for item in payload["items"]
        ))
        self.assertEqual(
            items[99]["factor_snapshot"][
                "bottom_divergence_v2_candidate_records"
            ][0]["unique_marker"],
            "record-99",
        )

    @patch("api.v1.endpoints.screening.ScreeningTaskService")
    def test_candidate_detail_returns_404_when_candidate_missing(self, service_cls) -> None:
        service = service_cls.return_value
        service.get_run.return_value = {
            "run_id": "run-20260313-1",
            "mode": "balanced",
            "status": "completed",
            "candidate_count": 1,
            "universe_size": 120,
        }
        service.get_candidate_detail.return_value = None

        response = self.client.get("/api/v1/screening/runs/run-20260313-1/candidates/999999")
        self.assertEqual(response.status_code, 404)

    @patch("api.v1.endpoints.screening.ScreeningTaskService")
    def test_candidate_detail_returns_409_when_run_not_completed(self, service_cls) -> None:
        service = service_cls.return_value
        service.get_run.return_value = {
            "run_id": "run-pending",
            "mode": "balanced",
            "status": "screening",
            "candidate_count": 1,
            "universe_size": 120,
        }

        response = self.client.get("/api/v1/screening/runs/run-pending/candidates/600519")
        self.assertEqual(response.status_code, 409)

    @patch("api.v1.endpoints.screening.ScreeningNotificationService")
    def test_notify_run_returns_success_payload(self, notify_service_cls) -> None:
        notify_service = notify_service_cls.return_value
        notify_service.notify_run.return_value = {
            "success": True,
            "notification_status": "sent",
            "run_id": "run-20260313-1",
            "candidate_count": 2,
            "report_path": "reports/screening_run_20260313_1.md",
        }

        response = self.client.post(
            "/api/v1/screening/runs/run-20260313-1/notify",
            json={"limit": 5, "with_ai_only": True, "force": False},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["data"]["run_id"], "run-20260313-1")
        notify_service.notify_run.assert_called_once_with(
            run_id="run-20260313-1",
            force=False,
        )

    @patch("api.v1.endpoints.screening.ScreeningNotificationService")
    def test_notify_run_returns_409_when_run_not_ready(self, notify_service_cls) -> None:
        notify_service = notify_service_cls.return_value
        from src.services.screening_notification_service import ScreeningRunNotReadyError

        notify_service.notify_run.side_effect = ScreeningRunNotReadyError("筛选任务尚未完成，暂不可推送")

        response = self.client.post(
            "/api/v1/screening/runs/run-pending/notify",
            json={"limit": 5, "with_ai_only": False},
        )

        self.assertEqual(response.status_code, 409)

    @patch("api.v1.endpoints.screening.ScreeningNotificationService")
    def test_notify_run_returns_404_when_run_missing(self, notify_service_cls) -> None:
        from src.services.screening_notification_service import ScreeningRunNotFoundError

        notify_service = notify_service_cls.return_value
        notify_service.notify_run.side_effect = ScreeningRunNotFoundError("筛选任务不存在")

        response = self.client.post(
            "/api/v1/screening/runs/missing-run/notify",
            json={"limit": 5, "with_ai_only": False},
        )

        self.assertEqual(response.status_code, 404)

    @patch("api.v1.endpoints.screening.ScreeningNotificationService")
    def test_notify_run_returns_skipped_when_already_sent(self, notify_service_cls) -> None:
        notify_service = notify_service_cls.return_value
        notify_service.notify_run.return_value = {
            "success": True,
            "skipped": True,
            "reason": "already_sent",
            "run_id": "run-20260313-1",
        }

        response = self.client.post(
            "/api/v1/screening/runs/run-20260313-1/notify",
            json={"limit": 5, "with_ai_only": False},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        # success=False because skipped=True
        self.assertFalse(payload["success"])
        self.assertEqual(payload["message"], "already_sent")

    def test_create_run_rejects_unsupported_market(self) -> None:
        response = self.client.post(
            "/api/v1/screening/runs",
            json={
                "trade_date": "2026-03-13",
                "stock_codes": ["600519"],
                "market": "us",
            },
        )

        self.assertEqual(response.status_code, 422)

    def test_create_run_rejects_empty_stock_codes_list(self) -> None:
        response = self.client.post(
            "/api/v1/screening/runs",
            json={
                "trade_date": "2026-03-13",
                "stock_codes": [],
                "market": "cn",
            },
        )

        self.assertEqual(response.status_code, 422)

    def test_create_run_rejects_blank_stock_codes_list(self) -> None:
        response = self.client.post(
            "/api/v1/screening/runs",
            json={
                "trade_date": "2026-03-13",
                "stock_codes": [" ", ""],
                "market": "cn",
            },
        )

        self.assertEqual(response.status_code, 422)

    def test_create_run_rejects_ai_top_k_greater_than_candidate_limit(self) -> None:
        response = self.client.post(
            "/api/v1/screening/runs",
            json={
                "trade_date": "2026-03-13",
                "stock_codes": ["600519"],
                "candidate_limit": 1,
                "ai_top_k": 5,
                "market": "cn",
            },
        )

        self.assertEqual(response.status_code, 422)

    def test_create_run_rejects_ai_top_k_greater_than_default_candidate_limit(self) -> None:
        response = self.client.post(
            "/api/v1/screening/runs",
            json={
                "trade_date": "2026-03-13",
                "stock_codes": ["600519"],
                "ai_top_k": 40,
                "market": "cn",
            },
        )

        self.assertEqual(response.status_code, 422)

    @patch("api.v1.endpoints.screening.ScreeningTaskService.execute_run")
    def test_create_run_accepts_ai_top_k_within_aggressive_mode_limit(self, execute_run) -> None:
        execute_run.return_value = {
            "run_id": "run-aggressive-boundary",
            "mode": "aggressive",
            "status": "completed",
            "candidate_count": 0,
            "universe_size": 1,
        }

        response = self.client.post(
            "/api/v1/screening/runs",
            json={
                "trade_date": "2026-03-13",
                "stock_codes": ["600519"],
                "mode": "aggressive",
                "ai_top_k": 40,
                "market": "cn",
            },
        )

        self.assertEqual(response.status_code, 200)

    def test_create_run_rejects_ai_top_k_above_aggressive_mode_limit(self) -> None:
        response = self.client.post(
            "/api/v1/screening/runs",
            json={
                "trade_date": "2026-03-13",
                "stock_codes": ["600519"],
                "mode": "aggressive",
                "ai_top_k": 51,
                "market": "cn",
            },
        )

        self.assertEqual(response.status_code, 422)

    @patch("api.v1.endpoints.screening.ScreeningTaskService.execute_run")
    def test_create_run_accepts_ai_top_k_at_balanced_mode_limit(self, execute_run) -> None:
        execute_run.return_value = {
            "run_id": "run-balanced-boundary",
            "mode": "balanced",
            "status": "completed",
            "candidate_count": 0,
            "universe_size": 1,
        }

        response = self.client.post(
            "/api/v1/screening/runs",
            json={
                "trade_date": "2026-03-13",
                "stock_codes": ["600519"],
                "mode": "balanced",
                "ai_top_k": 30,
                "market": "cn",
            },
        )

        self.assertEqual(response.status_code, 200)

    @patch("api.v1.endpoints.screening.ScreeningTaskService.execute_run")
    def test_create_run_accepts_ai_top_k_at_explicit_candidate_limit(self, execute_run) -> None:
        execute_run.return_value = {
            "run_id": "run-explicit-boundary",
            "mode": "balanced",
            "status": "completed",
            "candidate_count": 0,
            "universe_size": 1,
        }

        response = self.client.post(
            "/api/v1/screening/runs",
            json={
                "trade_date": "2026-03-13",
                "stock_codes": ["600519"],
                "candidate_limit": 5,
                "ai_top_k": 5,
                "market": "cn",
            },
        )

        self.assertEqual(response.status_code, 200)

    @patch("api.v1.endpoints.screening.ScreeningTaskService")
    def test_create_run_passes_mode_to_service(self, service_cls) -> None:
        service = service_cls.return_value
        service.config.screening_default_mode = "balanced"
        service.config.screening_candidate_limit = 30
        service.config.screening_ai_top_k = 5
        service.resolve_run_config.return_value.candidate_limit = 50
        service.resolve_run_config.return_value.ai_top_k = 8
        service.execute_run.return_value = {
            "run_id": "run-20260313-4",
            "mode": "aggressive",
            "status": "completed",
            "candidate_count": 3,
            "universe_size": 100,
        }

        response = self.client.post(
            "/api/v1/screening/runs",
            json={
                "trade_date": "2026-03-13",
                "stock_codes": ["600519"],
                "mode": "aggressive",
                "market": "cn",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "aggressive")
        self.assertEqual(service.execute_run.call_args.kwargs["mode"], "aggressive")

    @patch("api.v1.endpoints.screening.ScreeningTaskService")
    def test_create_run_passes_rerun_controls_to_service(self, service_cls) -> None:
        service = service_cls.return_value
        service.config.screening_default_mode = "balanced"
        service.config.screening_candidate_limit = 30
        service.config.screening_ai_top_k = 5
        service.resolve_run_config.return_value.candidate_limit = 30
        service.resolve_run_config.return_value.ai_top_k = 5
        service.execute_run.return_value = {
            "run_id": "run-20260313-5",
            "mode": "balanced",
            "status": "completed",
            "candidate_count": 1,
            "universe_size": 10,
        }

        response = self.client.post(
            "/api/v1/screening/runs",
            json={
                "trade_date": "2026-03-13",
                "rerun_failed": True,
                "resume_from": "factorizing",
                "market": "cn",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(service.execute_run.call_args.kwargs["rerun_failed"])
        self.assertEqual(service.execute_run.call_args.kwargs["resume_from"], "factorizing")

    @patch("api.v1.endpoints.screening.ScreeningTaskService")
    def test_create_run_returns_422_when_rerun_controls_are_invalid(self, service_cls) -> None:
        service = service_cls.return_value
        service.config.screening_default_mode = "balanced"
        service.config.screening_candidate_limit = 30
        service.config.screening_ai_top_k = 5
        service.resolve_run_config.return_value.candidate_limit = 30
        service.resolve_run_config.return_value.ai_top_k = 5
        service.execute_run.side_effect = ValueError("resume_from 仅支持失败任务补跑")

        response = self.client.post(
            "/api/v1/screening/runs",
            json={
                "trade_date": "2026-03-13",
                "resume_from": "factorizing",
                "market": "cn",
            },
        )

        self.assertEqual(response.status_code, 422)
        payload = response.json()
        self.assertEqual(payload["error"], "validation_error")

    @patch("api.v1.endpoints.screening.ScreeningTaskService")
    def test_create_run_returns_specific_error_code_when_today_data_is_not_ready(self, service_cls) -> None:
        service = service_cls.return_value
        service.config.screening_default_mode = "balanced"
        service.config.screening_candidate_limit = 30
        service.config.screening_ai_top_k = 5
        service.resolve_run_config.return_value.candidate_limit = 30
        service.resolve_run_config.return_value.ai_top_k = 5
        service.execute_run.side_effect = ScreeningTradeDateNotReadyError(
            "当前时间未到 15:00（Asia/Shanghai），今日 A 股日线数据未完全收盘，请选择上一交易日或 15:00 后再试。"
        )

        response = self.client.post(
            "/api/v1/screening/runs",
            json={
                "trade_date": "2026-03-13",
                "market": "cn",
            },
        )

        self.assertEqual(response.status_code, 422)
        payload = response.json()
        self.assertEqual(payload["error"], "screening_trade_time_not_ready")

    @patch("api.v1.endpoints.screening.ScreeningTaskService")
    def test_delete_run_returns_success_response(self, service_cls) -> None:
        service = service_cls.return_value
        service.delete_run.return_value = True

        response = self.client.delete("/api/v1/screening/runs/run-123")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertIn("run-123", payload["message"])

    @patch("api.v1.endpoints.screening.ScreeningTaskService")
    def test_delete_run_returns_404_when_missing(self, service_cls) -> None:
        service = service_cls.return_value
        service.delete_run.return_value = False

        response = self.client.delete("/api/v1/screening/runs/run-missing")

        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertEqual(payload["error"], "not_found")


if __name__ == "__main__":
    unittest.main()
