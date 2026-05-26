import unittest
from unittest.mock import MagicMock, call

from src.services.screening_notification_service import (
    ScreeningNotificationDeliveryError,
    ScreeningNotificationService,
)


class ScreeningNotificationServiceTestCase(unittest.TestCase):
    def test_build_run_notification_shows_explicit_empty_result(self) -> None:
        service = ScreeningNotificationService(
            screening_task_service=MagicMock(),
            notifier=MagicMock(),
        )

        content = service.build_run_notification(
            run={
                "run_id": "run-empty-001",
                "trade_date": "2026-05-18",
                "mode": "balanced",
                "status": "completed",
                "universe_size": 5300,
                "candidate_count": 0,
            },
            candidates=[],
        )

        self.assertIn("## 今日结果", content)
        self.assertIn("本次筛选未产生可推送候选", content)
        self.assertNotIn("## Top 推荐", content)

    def test_build_run_notification_treats_mock_candidate_iterable_as_empty(self) -> None:
        service = ScreeningNotificationService(
            screening_task_service=MagicMock(),
            notifier=MagicMock(),
        )

        content = service.build_run_notification(
            run={
                "run_id": "run-empty-mock",
                "trade_date": "2026-05-18",
                "mode": "balanced",
                "status": "completed",
                "universe_size": 0,
                "candidate_count": 0,
            },
            candidates=MagicMock(),
        )

        self.assertIn("本次筛选未产生可推送候选", content)
        self.assertNotIn("## Top 推荐", content)

    def test_build_run_notification_contains_rules_and_ai_sections(self) -> None:
        service = ScreeningNotificationService(
            screening_task_service=MagicMock(),
            notifier=MagicMock(),
        )

        content = service.build_run_notification(
            run={
                "run_id": "run-001",
                "trade_date": "2026-03-13",
                "mode": "balanced",
                "status": "completed_with_ai_degraded",
                "universe_size": 5000,
                "candidate_count": 2,
            },
            candidates=[
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "final_rank": 1,
                    "rule_score": 91.5,
                    "final_score": 96.5,
                    "recommendation_source": "rules_plus_ai",
                    "recommendation_reason": "规则得分 91.5；AI 建议 关注；新闻补充 1 条",
                    "ai_summary": "趋势未破坏。",
                    "news_summary": "贵州茅台新品上市",
                },
                {
                    "code": "000001",
                    "name": "平安银行",
                    "final_rank": 2,
                    "rule_score": 82.0,
                    "final_score": 82.0,
                    "recommendation_source": "rules_only",
                    "recommendation_reason": "规则得分 82.0；按规则结果输出",
                },
            ],
        )

        self.assertIn("全市场筛选推荐名单", content)
        self.assertIn("AI 二筛已降级", content)
        self.assertIn("贵州茅台", content)
        self.assertIn("趋势未破坏", content)
        self.assertIn("平安银行", content)
        self.assertIn("规则输出", content)

    def test_build_run_notification_contains_frontend_like_candidate_details(self) -> None:
        service = ScreeningNotificationService(
            screening_task_service=MagicMock(),
            notifier=MagicMock(),
        )

        content = service.build_run_notification(
            run={
                "run_id": "run-detail-001",
                "trade_date": "2026-05-18",
                "mode": "balanced",
                "status": "completed",
                "universe_size": 5000,
                "candidate_count": 1,
            },
            candidates=[
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "final_rank": 1,
                    "rule_score": 92.5,
                    "final_score": 98.5,
                    "recommendation_source": "rules_plus_ai",
                    "rule_hits": ["trend_aligned", "volume_expanding"],
                    "matched_strategies": ["extreme_strength_combo"],
                    "trade_stage": "probe_entry",
                    "setup_type": "trend_breakout",
                    "entry_maturity": "high",
                    "setup_freshness": 0.8,
                    "setup_hit_reasons": ["突破MA100", "放量确认"],
                    "risk_level": "medium",
                    "market_regime": "balanced",
                    "market_message": "指数站上MA100",
                    "theme_position": "main_theme",
                    "theme_tag": "白酒",
                    "theme_score": 88,
                    "theme_duration": "3天",
                    "leader_stocks": ["600519", "000858"],
                    "candidate_pool_level": "leader_pool",
                    "trade_plan": {
                        "initial_position": "1/5仓",
                        "stop_loss_rule": "跌破突破K线低点",
                        "add_rule": "回踩MA20不破加仓",
                        "take_profit_plan": "沿MA10移动止盈",
                        "invalidation_rule": "三日不启动离场",
                        "holding_expectation": "1~2周",
                        "execution_note": "等待回踩确认",
                    },
                    "ai_trade_stage": "probe_entry",
                    "ai_confidence": 0.82,
                    "ai_reasoning": "量价结构较强",
                    "ai_operation_advice": "关注",
                    "ai_summary": "趋势未破坏。",
                    "factor_snapshot": {
                        "primary_theme": "白酒",
                        "sector": "食品饮料",
                        "industry": "白酒",
                        "theme_heat_score": 91,
                        "theme_pool_score": 22,
                        "leadership_score": 28,
                        "entry_signal_score": 31,
                        "timing_penalty": -4,
                        "extreme_strength_score": 77,
                        "leader_double_count": 6,
                        "extreme_strength_score_deduplicated": 71,
                        "stage_label": "breakout_day",
                        "primary_signal": "limitup_structure",
                        "signal_kind": "momentum_breakout",
                        "timing_reasons": ["突破当日", "未明显走远"],
                        "phase_explanations": [
                            {"label": "趋势结构", "hit": True, "summary": "MA 多头排列"},
                            {"label": "量能确认", "hit": True, "summary": "量比放大"},
                        ],
                        "risk_params": {
                            "stop_loss": 1520,
                            "stop_loss_basis": "突破K线低点",
                            "position_size": "1/5仓",
                            "take_profit_ratio": 0.18,
                        },
                    },
                }
            ],
        )

        self.assertIn("L1 大盘环境", content)
        self.assertIn("L2 题材/板块", content)
        self.assertIn("食品饮料", content)
        self.assertIn("白酒", content)
        self.assertIn("分层评分", content)
        self.assertIn("主题池 22", content)
        self.assertIn("L4 入场信号", content)
        self.assertIn("突破MA100", content)
        self.assertIn("阶段命中明细", content)
        self.assertIn("MA 多头排列", content)
        self.assertIn("L5 交易计划", content)
        self.assertIn("跌破突破K线低点", content)
        self.assertIn("风险参数", content)
        self.assertIn("突破K线低点", content)
        self.assertIn("AI 复核", content)
        self.assertIn("量价结构较强", content)

    def test_build_run_notification_uses_ai_trade_plan_when_rule_plan_missing(self) -> None:
        service = ScreeningNotificationService(
            screening_task_service=MagicMock(),
            notifier=MagicMock(),
        )

        content = service.build_run_notification(
            run={
                "run_id": "run-ai-plan",
                "trade_date": "2026-05-18",
                "mode": "balanced",
                "status": "completed",
                "universe_size": 5000,
                "candidate_count": 1,
            },
            candidates=[
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "final_rank": 1,
                    "rule_score": "N/A",
                    "final_score": "-",
                    "trade_stage": "probe_entry",
                    "setup_type": "trend_breakout",
                    "initial_position": "1/6仓",
                    "stop_loss_rule": "跌破AI止损位",
                    "take_profit_plan": "分批止盈",
                    "invalidation_rule": "三日不启动",
                    "ai_reasoning": "AI 给出执行计划",
                    "rule_hits": "not-json",
                    "factor_snapshot": "{bad-json",
                }
            ],
        )

        self.assertIn("1/6仓", content)
        self.assertIn("跌破AI止损位", content)
        self.assertIn("分批止盈", content)
        self.assertIn("三日不启动", content)
        self.assertIn("AI 给出执行计划", content)

    def test_send_run_notification_uses_notification_service_and_saves_report(self) -> None:
        screening_task_service = MagicMock()
        screening_task_service.get_run.return_value = {
            "run_id": "run-001",
            "trade_date": "2026-03-13",
            "mode": "balanced",
            "status": "completed",
            "universe_size": 5000,
            "candidate_count": 1,
        }
        screening_task_service.list_candidates.return_value = [
            {
                "code": "600519",
                "name": "贵州茅台",
                "final_rank": 1,
                "rule_score": 91.5,
                "final_score": 96.5,
                "recommendation_source": "rules_plus_ai",
                "recommendation_reason": "规则得分 91.5；AI 建议 关注",
            }
        ]

        notifier = MagicMock()
        notifier.send.return_value = True
        notifier.save_report_to_file.return_value = "reports/screening_run_001.md"

        service = ScreeningNotificationService(
            screening_task_service=screening_task_service,
            notifier=notifier,
        )

        result = service.send_run_notification(run_id="run-001", limit=5, with_ai_only=False)

        self.assertTrue(result["success"])
        self.assertEqual(result["run_id"], "run-001")
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["report_path"], "reports/screening_run_001.md")
        notifier.send.assert_called_once()
        self.assertEqual(notifier.send.call_args.kwargs["email_stock_codes"], ["600519"])
        notifier.save_report_to_file.assert_called_once()

    def test_send_run_notification_raises_when_delivery_fails_after_report_saved(self) -> None:
        screening_task_service = MagicMock()
        screening_task_service.get_run.return_value = {
            "run_id": "run-001",
            "trade_date": "2026-03-13",
            "mode": "balanced",
            "status": "completed",
            "universe_size": 5000,
            "candidate_count": 1,
        }
        screening_task_service.list_candidates.return_value = [
            {
                "code": "600519",
                "name": "贵州茅台",
                "final_rank": 1,
                "rule_score": 91.5,
                "final_score": 96.5,
                "recommendation_source": "rules_plus_ai",
                "recommendation_reason": "规则得分 91.5；AI 建议 关注",
            }
        ]

        notifier = MagicMock()
        notifier.send.return_value = False
        notifier.save_report_to_file.return_value = "reports/screening_run_001.md"

        service = ScreeningNotificationService(
            screening_task_service=screening_task_service,
            notifier=notifier,
        )

        with self.assertRaises(ScreeningNotificationDeliveryError):
            service.send_run_notification(run_id="run-001", limit=5, with_ai_only=False)

        notifier.save_report_to_file.assert_called_once()
        notifier.send.assert_called_once()
        self.assertLess(
            notifier.mock_calls.index(call.save_report_to_file(unittest.mock.ANY, filename="screening_run-001.md")),
            notifier.mock_calls.index(call.send(unittest.mock.ANY, email_stock_codes=["600519"])),
        )


if __name__ == "__main__":
    unittest.main()
