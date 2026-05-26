import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.services.screening_schedule_service import ScreeningScheduleService


class ScreeningScheduleServiceTestCase(unittest.TestCase):
    def _build_config(self):
        return SimpleNamespace(
            trading_day_check_enabled=True,
            screening_default_mode="balanced",
            screening_candidate_limit=30,
            screening_ai_top_k=5,
        )

    @patch("src.services.screening_schedule_service.get_open_markets_today", return_value=set())
    def test_run_once_skips_when_target_market_is_closed(self, _mock_open_markets) -> None:
        task_service = MagicMock()
        service = ScreeningScheduleService(config=self._build_config(), screening_task_service=task_service)

        result = service.run_once(force_run=False, market="cn")

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "non_trading_day")
        task_service.execute_run.assert_not_called()

    @patch("src.services.screening_schedule_service.get_open_markets_today", return_value={"cn"})
    def test_run_once_executes_screening_on_open_market(self, _mock_open_markets) -> None:
        task_service = MagicMock()
        task_service.execute_run.return_value = {"run_id": "run-20260313-cn-balanced", "status": "completed"}
        service = ScreeningScheduleService(
            config=self._build_config(),
            screening_task_service=task_service,
        )

        result = service.run_once(force_run=False, market="cn")

        self.assertEqual(result["run_id"], "run-20260313-cn-balanced")
        task_service.execute_run.assert_called_once_with(
            trade_date=None,
            mode="balanced",
            candidate_limit=30,
            ai_top_k=5,
            market="cn",
            trigger_type="scheduled",
        )


if __name__ == "__main__":
    unittest.main()
