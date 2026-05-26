"""RED phase: Tests for notify_run service logic.

These tests verify the idempotent notification workflow:
- can_notify gate checks
- pending run triggers notification
- already-sent run is skipped
- failed run allows retry
- 0 candidates still sends notification
- legacy skipped run requires force
"""
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from src.services.screening_notification_service import (
    ScreeningNotificationService,
    ScreeningRunNotFoundError,
    ScreeningRunNotReadyError,
)


class NotifyRunIdempotencyTestCase(unittest.TestCase):
    """Tests for notify_run() idempotent behavior."""

    def _build_service(
        self,
        run: dict | None = None,
        candidates: list | None = None,
        send_success: bool = True,
    ) -> ScreeningNotificationService:
        task_service = MagicMock()
        task_service.get_run.return_value = run
        task_service.list_candidates.return_value = candidates or []
        notifier = MagicMock()
        notifier.send.return_value = send_success
        notifier.save_report_to_file.return_value = "reports/screening_run-001.md"
        db_manager = MagicMock()
        db_manager.update_notification_status.return_value = True
        service = ScreeningNotificationService(
            screening_task_service=task_service,
            notifier=notifier,
            db_manager=db_manager,
        )
        return service

    def _completed_run(self, **overrides) -> dict:
        base = {
            "run_id": "run-001",
            "trade_date": "2026-03-15",
            "mode": "aggressive",
            "status": "completed",
            "universe_size": 5000,
            "candidate_count": 3,
            "trigger_type": "scheduled",
            "notification_status": "pending",
            "notification_attempts": 0,
            "notification_sent_at": None,
            "notification_error": None,
        }
        base.update(overrides)
        return base

    # -- notify_run: pending -> sent --

    def test_notify_run_sends_for_pending_scheduled_run(self) -> None:
        run = self._completed_run(notification_status="pending")
        service = self._build_service(run=run, candidates=[{"code": "600519", "name": "T", "final_rank": 1, "rule_score": 90, "final_score": 95}])
        result = service.notify_run("run-001")
        self.assertTrue(result["success"])
        self.assertEqual(result["notification_status"], "sent")
        service.screening_task_service.list_candidates.assert_called_once_with(
            run_id="run-001",
            limit=100,
        )

    # -- notify_run: archive uses full audit, push prefers full audit (adaptive) --

    def test_notify_run_pushes_full_audit_when_within_size_limit(self) -> None:
        """少量候选时，推送内容应保留每只候选的完整审计块，便于在飞书折叠面板里看到详情。"""
        run = self._completed_run(notification_status="pending")
        candidates = [
            {
                "code": "600519",
                "name": "贵州茅台",
                "final_rank": 1,
                "rule_score": 90,
                "final_score": 95,
            }
        ]
        service = self._build_service(run=run, candidates=candidates)
        result = service.notify_run("run-001")
        self.assertTrue(result["success"])

        archive_args, _ = service.notifier.save_report_to_file.call_args
        archive_content = archive_args[0]
        send_args, _ = service.notifier.send.call_args
        push_content = send_args[0]

        # 文件存档与推送内容均应包含完整审计标题（[评分汇总] / [规则分拆解] / [原始指标]）。
        for marker in ("[评分汇总]", "[规则分拆解]", "[原始指标]", "[审计证据]"):
            with self.subTest(marker=marker):
                self.assertIn(marker, archive_content)
                self.assertIn(marker, push_content)
        # 推送内容仍然展示候选名称 / 代码。
        self.assertIn("贵州茅台", push_content)
        self.assertIn("600519", push_content)

    def test_notify_run_downgrades_audit_top_n_when_exceeding_size_limit(self) -> None:
        """候选过多时，推送内容应自动降级 audit_top_n 以控制单条飞书消息字节数。

        模拟 30 只候选 × 每只完整审计 ~2KB ≈ 60KB（远超 28KB），自适应降级应将其压到 28KB 以内，
        同时 30 只候选的紧凑摘要部分仍能出现在内容里。
        """
        run = self._completed_run(notification_status="pending", candidate_count=30)
        candidates = [
            {
                "code": f"60{i:04d}",
                "name": f"候选样本股票{i:02d}",
                "final_rank": i,
                "rule_score": 90.0,
                "final_score": 95.0,
                "trade_stage": "focus",
                "rule_hits_json": (
                    '["trend_aligned","volume_expanding","near_breakout","liquidity_ok"]'
                ),
                "factor_snapshot_json": (
                    '{"close":51.72,"ma5":51.18,"ma10":52.43,"ma20":49.5,'
                    '"volume_ratio":2.6,"breakout_ratio":1.1739,"avg_amount":830000000.0,'
                    '"days_since_listed":4200,"is_st":false}'
                ),
                "ai_operation_advice": "关注",
                "ai_summary": "趋势未破坏，继续持有。",
                "news_count": 2,
                "news_summary": "近期热点催化。",
                "has_ai_analysis": True,
                "recommendation_source": "rules_plus_ai",
            }
            for i in range(1, 31)
        ]
        service = self._build_service(run=run, candidates=candidates)
        result = service.notify_run("run-001")
        self.assertTrue(result["success"])

        send_args, _ = service.notifier.send.call_args
        push_content = send_args[0]

        # 推送内容必须落在 28KB 单条上限内（adaptive ladder 收紧策略）。
        push_bytes = len(push_content.encode("utf-8"))
        self.assertLessEqual(push_bytes, 28000, f"push content bytes={push_bytes}")
        # 即使降级，紧凑摘要也仍能展示首尾候选，让用户在折叠面板里浏览全部命中。
        self.assertIn("候选样本股票01", push_content)
        self.assertIn("候选样本股票30", push_content)

    # -- notify_run: already sent, force=False -> skip --

    def test_notify_run_skips_already_sent_without_force(self) -> None:
        run = self._completed_run(notification_status="sent", notification_sent_at="2026-03-15T10:00:00")
        service = self._build_service(run=run)
        result = service.notify_run("run-001", force=False)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "already_sent")

    # -- notify_run: failed -> retry --

    def test_notify_run_retries_failed_run(self) -> None:
        run = self._completed_run(notification_status="failed", notification_attempts=1, notification_error="timeout")
        service = self._build_service(run=run, candidates=[])
        result = service.notify_run("run-001")
        self.assertTrue(result["success"])

    # -- notify_run: skipped + force=True -> send --

    def test_notify_run_force_sends_skipped_run(self) -> None:
        run = self._completed_run(trigger_type="manual", notification_status="skipped")
        service = self._build_service(run=run, candidates=[])
        result = service.notify_run("run-001", force=True)
        self.assertTrue(result["success"])

    # -- notify_run: sent + force=True -> reject (v1 rule) --

    def test_notify_run_rejects_force_resend_of_sent(self) -> None:
        run = self._completed_run(notification_status="sent", notification_sent_at="2026-03-15T10:00:00")
        service = self._build_service(run=run)
        result = service.notify_run("run-001", force=True)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "already_sent")

    # -- notify_run: 0 candidates sends notification --

    def test_notify_run_sends_with_zero_candidates(self) -> None:
        run = self._completed_run(candidate_count=0, notification_status="pending")
        service = self._build_service(run=run, candidates=[])
        result = service.notify_run("run-001")
        self.assertTrue(result["success"])

    # -- notify_run: run not found --

    def test_notify_run_raises_when_run_not_found(self) -> None:
        service = self._build_service(run=None)
        with self.assertRaises(ScreeningRunNotFoundError):
            service.notify_run("run-nonexist")

    # -- notify_run: run not completed --

    def test_notify_run_raises_when_run_not_completed(self) -> None:
        run = self._completed_run(status="screening")
        service = self._build_service(run=run)
        with self.assertRaises(ScreeningRunNotReadyError):
            service.notify_run("run-001")

    # -- notify_run: delivery failure -> mark failed --

    def test_notify_run_marks_failed_on_delivery_error(self) -> None:
        run = self._completed_run(notification_status="pending")
        service = self._build_service(run=run, candidates=[], send_success=False)
        result = service.notify_run("run-001")
        self.assertFalse(result["success"])
        self.assertEqual(result["notification_status"], "failed")


class CanNotifyTestCase(unittest.TestCase):
    """Tests for can_notify() gate logic."""

    def _make_run(self, **overrides) -> dict:
        base = {
            "status": "completed",
            "notification_status": "pending",
        }
        base.update(overrides)
        return base

    def test_can_notify_pending(self) -> None:
        result = ScreeningNotificationService.can_notify(self._make_run(), force=False)
        self.assertTrue(result["allowed"])

    def test_can_notify_failed(self) -> None:
        result = ScreeningNotificationService.can_notify(self._make_run(notification_status="failed"), force=False)
        self.assertTrue(result["allowed"])

    def test_cannot_notify_sent_without_force(self) -> None:
        result = ScreeningNotificationService.can_notify(self._make_run(notification_status="sent"), force=False)
        self.assertFalse(result["allowed"])

    def test_cannot_notify_sent_even_with_force(self) -> None:
        result = ScreeningNotificationService.can_notify(self._make_run(notification_status="sent"), force=True)
        self.assertFalse(result["allowed"])

    def test_can_notify_skipped_with_force(self) -> None:
        result = ScreeningNotificationService.can_notify(self._make_run(notification_status="skipped"), force=True)
        self.assertTrue(result["allowed"])

    def test_cannot_notify_skipped_without_force(self) -> None:
        result = ScreeningNotificationService.can_notify(self._make_run(notification_status="skipped"), force=False)
        self.assertFalse(result["allowed"])

    def test_cannot_notify_non_completed_run(self) -> None:
        result = ScreeningNotificationService.can_notify(self._make_run(status="screening"), force=False)
        self.assertFalse(result["allowed"])


if __name__ == "__main__":
    unittest.main()
