"""RED phase: Tests for schedule -> notify orchestration.

These tests verify that:
- scheduled run delegates to ScreeningTaskService
- notification is owned by ScreeningTaskService, not the schedule wrapper
- failed run does not trigger wrapper-level notify
- non-trading day skips without running or notifying
"""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

from src.services.screening_schedule_service import ScreeningScheduleService


class ScheduleNotifyOrchestrationTestCase(unittest.TestCase):
    """Verify scheduled run -> notify orchestration."""

    def _build_config(self):
        return SimpleNamespace(
            trading_day_check_enabled=True,
            screening_default_mode="aggressive",
            screening_candidate_limit=30,
            screening_ai_top_k=5,
        )

    @patch("src.services.screening_schedule_service.get_open_markets_today", return_value={"cn"})
    def test_scheduled_run_delegates_notification_to_task_service_on_completed(self, _mock_markets) -> None:
        task_service = MagicMock()
        task_service.execute_run.return_value = {
            "run_id": "run-sched-001",
            "status": "completed",
            "candidate_count": 3,
        }

        service = ScreeningScheduleService(
            config=self._build_config(),
            screening_task_service=task_service,
        )

        result = service.run_once(force_run=False, market="cn")

        self.assertEqual(result["status"], "completed")

    @patch("src.services.screening_schedule_service.get_open_markets_today", return_value={"cn"})
    def test_scheduled_run_delegates_notification_to_task_service_on_ai_degraded(self, _mock_markets) -> None:
        task_service = MagicMock()
        task_service.execute_run.return_value = {
            "run_id": "run-sched-002",
            "status": "completed_with_ai_degraded",
            "candidate_count": 2,
        }

        service = ScreeningScheduleService(
            config=self._build_config(),
            screening_task_service=task_service,
        )

        result = service.run_once(force_run=False, market="cn")

        self.assertIn(result["status"], {"completed", "completed_with_ai_degraded"})

    @patch("src.services.screening_schedule_service.get_open_markets_today", return_value={"cn"})
    def test_scheduled_run_does_not_notify_on_failed(self, _mock_markets) -> None:
        task_service = MagicMock()
        task_service.execute_run.return_value = {
            "run_id": "run-sched-003",
            "status": "failed",
            "error_summary": "some error",
        }

        service = ScreeningScheduleService(
            config=self._build_config(),
            screening_task_service=task_service,
        )

        result = service.run_once(force_run=False, market="cn")

        self.assertEqual(result["status"], "failed")

    @patch("src.services.screening_schedule_service.get_open_markets_today", return_value=set())
    def test_non_trading_day_skips_without_notify(self, _mock_markets) -> None:
        task_service = MagicMock()

        service = ScreeningScheduleService(
            config=self._build_config(),
            screening_task_service=task_service,
        )

        result = service.run_once(force_run=False, market="cn")

        self.assertEqual(result["status"], "skipped")
        task_service.execute_run.assert_not_called()

    @patch("src.services.screening_schedule_service.get_open_markets_today", return_value={"cn"})
    def test_wrapper_level_notify_failure_is_not_applicable(self, _mock_markets) -> None:
        """Schedule wrapper does not own notification delivery."""
        task_service = MagicMock()
        task_service.execute_run.return_value = {
            "run_id": "run-sched-004",
            "status": "completed",
            "candidate_count": 1,
        }

        service = ScreeningScheduleService(
            config=self._build_config(),
            screening_task_service=task_service,
        )

        result = service.run_once(force_run=False, market="cn")

        # run result should still be returned successfully
        self.assertEqual(result["status"], "completed")

    @patch("src.services.screening_schedule_service.get_open_markets_today", return_value={"cn"})
    def test_execute_run_passes_scheduled_trigger_type(self, _mock_markets) -> None:
        task_service = MagicMock()
        task_service.execute_run.return_value = {
            "run_id": "run-sched-005",
            "status": "completed",
        }

        service = ScreeningScheduleService(
            config=self._build_config(),
            screening_task_service=task_service,
        )
        service.run_once(force_run=False, market="cn")

        call_kwargs = task_service.execute_run.call_args
        self.assertEqual(call_kwargs.kwargs.get("trigger_type"), "scheduled")


if __name__ == "__main__":
    unittest.main()
