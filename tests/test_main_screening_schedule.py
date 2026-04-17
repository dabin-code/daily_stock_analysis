import argparse
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

for optional_module in ("json_repair",):
    if optional_module not in sys.modules:
        sys.modules[optional_module] = mock.MagicMock()

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

import main as main_module


class MainScreeningScheduleTestCase(unittest.TestCase):
    @staticmethod
    def _build_config(**overrides):
        config = SimpleNamespace(
            log_dir="./logs",
            validate=lambda: [],
            webui_enabled=False,
            schedule_enabled=False,
            schedule_time="18:00",
            schedule_run_immediately=True,
            board_sync_schedule_enabled=False,
            board_sync_schedule_time="15:05",
            board_sync_run_immediately=False,
            kline_governance_enabled=False,
            kline_governance_schedule_time="17:00",
            kline_governance_run_immediately=False,
            kline_deep_audit_schedule_enabled=False,
            kline_deep_audit_schedule_time="17:00",
            run_immediately=False,
        )
        for key, value in overrides.items():
            setattr(config, key, value)
        return config

    @staticmethod
    def _build_args(**overrides):
        base = dict(
            debug=False,
            stocks=None,
            webui=False,
            webui_only=False,
            serve=False,
            serve_only=False,
            host="0.0.0.0",
            port=8000,
            backtest=False,
            market_review=False,
            schedule=False,
            no_run_immediately=False,
            screening=False,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_main_runs_screening_once_when_screening_flag_enabled(self) -> None:
        args = self._build_args(screening=True)
        config = self._build_config(schedule_enabled=True)

        with patch.object(main_module, "setup_logging"), patch.object(
            main_module, "parse_arguments", return_value=args
        ), patch.object(
            main_module, "get_config", return_value=config
        ), patch.object(main_module, "run_screening_workflow", return_value={"status": "completed"}) as mock_run:
            exit_code = main_module.main()

        self.assertEqual(exit_code, 0)
        mock_run.assert_called_once_with(config=config, args=args)

    def test_run_screening_workflow_raises_when_screening_failed(self) -> None:
        args = self._build_args(screening=True)
        config = self._build_config()

        fake_service = mock.MagicMock()
        fake_service.run_once.return_value = {
            "run_id": "run-failed",
            "status": "failed",
            "error_summary": "同步失败",
        }

        with patch("src.services.screening_schedule_service.ScreeningScheduleService", return_value=fake_service):
            with self.assertRaisesRegex(RuntimeError, "同步失败"):
                main_module.run_screening_workflow(config=config, args=args)

    def test_run_screening_workflow_raises_when_existing_run_is_still_running(self) -> None:
        args = self._build_args(screening=True)
        config = self._build_config()

        fake_service = mock.MagicMock()
        fake_service.run_once.return_value = {
            "run_id": "run-existing",
            "status": "screening",
        }

        with patch("src.services.screening_schedule_service.ScreeningScheduleService", return_value=fake_service):
            with self.assertRaisesRegex(RuntimeError, "screening"):
                main_module.run_screening_workflow(config=config, args=args)

    def test_run_kline_governance_workflow_raises_when_not_passed(self) -> None:
        args = self._build_args(schedule=True)
        config = self._build_config(kline_governance_enabled=True)

        fake_service = mock.MagicMock()
        fake_service.run_daily_governance.return_value = {
            "trade_date": "2026-04-17",
            "run_result": "degraded",
            "pass_status": "not_passed",
        }

        with patch(
            "src.services.kline_governance_schedule_service.KlineGovernanceScheduleService",
            return_value=fake_service,
        ):
            with self.assertRaisesRegex(RuntimeError, "K-line governance failed"):
                main_module.run_kline_governance_workflow(config=config, args=args)

    def test_run_kline_governance_workflow_returns_skipped_on_non_trading_day(self) -> None:
        args = self._build_args(schedule=True)
        config = self._build_config(kline_governance_enabled=True)

        fake_service = mock.MagicMock()
        fake_service.run_daily_governance.return_value = {
            "trade_date": None,
            "run_result": "skipped",
            "pass_status": "skipped",
            "reason": "non_trading_day",
        }

        with patch(
            "src.services.kline_governance_schedule_service.KlineGovernanceScheduleService",
            return_value=fake_service,
        ):
            result = main_module.run_kline_governance_workflow(config=config, args=args)

        self.assertEqual(result["run_result"], "skipped")
        self.assertEqual(result["pass_status"], "skipped")

    def test_run_kline_deep_audit_workflow_raises_when_not_passed(self) -> None:
        args = self._build_args(schedule=True)
        config = self._build_config(kline_deep_audit_schedule_enabled=True)

        fake_service = mock.MagicMock()
        fake_service.run_deep_audit.return_value = {
            "trade_date": "2026-04-17",
            "run_result": "degraded",
            "pass_status": "not_passed",
        }

        with patch(
            "src.services.kline_governance_schedule_service.KlineGovernanceScheduleService",
            return_value=fake_service,
        ):
            with self.assertRaisesRegex(RuntimeError, "K-line deep audit failed"):
                main_module.run_kline_deep_audit_workflow(config=config, args=args)

    def test_run_kline_deep_audit_workflow_returns_skipped_on_non_trading_day(self) -> None:
        args = self._build_args(schedule=True)
        config = self._build_config(kline_deep_audit_schedule_enabled=True)

        fake_service = mock.MagicMock()
        fake_service.run_deep_audit.return_value = {
            "trade_date": None,
            "run_result": "skipped",
            "pass_status": "skipped",
            "reason": "non_trading_day",
        }

        with patch(
            "src.services.kline_governance_schedule_service.KlineGovernanceScheduleService",
            return_value=fake_service,
        ):
            result = main_module.run_kline_deep_audit_workflow(config=config, args=args)

        self.assertEqual(result["run_result"], "skipped")
        self.assertEqual(result["pass_status"], "skipped")

    def test_main_schedules_screening_workflow_when_schedule_mode_enabled(self) -> None:
        args = self._build_args(screening=True, schedule=True)
        config = self._build_config()
        scheduler = mock.MagicMock()

        with patch.object(main_module, "setup_logging"), patch.object(
            main_module, "parse_arguments", return_value=args
        ), patch.object(
            main_module, "get_config", return_value=config
        ), patch.object(main_module, "run_screening_workflow", return_value={"status": "completed"}) as mock_run, patch(
            "src.scheduler.Scheduler", return_value=scheduler
        ):
            exit_code = main_module.main()

        self.assertEqual(exit_code, 0)
        scheduler.add_daily_task.assert_called_once_with(
            name="screening",
            task=mock.ANY,
            schedule_time="18:00",
            run_immediately=False,
        )
        scheduler.run.assert_called_once()
        self.assertEqual(mock_run.call_count, 1)

    def test_main_registers_board_sync_job_when_enabled(self) -> None:
        args = self._build_args(screening=True, schedule=True)
        config = self._build_config(
            board_sync_schedule_enabled=True,
            board_sync_schedule_time="15:05",
            board_sync_run_immediately=False,
        )
        scheduler = mock.MagicMock()

        with patch.object(main_module, "setup_logging"), patch.object(
            main_module, "parse_arguments", return_value=args
        ), patch.object(
            main_module, "get_config", return_value=config
        ), patch.object(main_module, "run_screening_workflow", return_value={"status": "completed"}), patch.object(
            main_module, "run_board_sync_workflow", return_value={"status": "completed"}
        ), patch("src.scheduler.Scheduler", return_value=scheduler):
            exit_code = main_module.main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(scheduler.add_daily_task.call_count, 2)
        scheduler.add_daily_task.assert_any_call(
            name="screening",
            task=mock.ANY,
            schedule_time="18:00",
            run_immediately=False,
        )
        scheduler.add_daily_task.assert_any_call(
            name="board_sync",
            task=mock.ANY,
            schedule_time="15:05",
            run_immediately=False,
        )
        scheduler.run.assert_called_once()

    def test_kline_governance_schedule_runs_at_1700_cn_market_only(self) -> None:
        args = self._build_args(schedule=True)
        config = self._build_config(
            kline_governance_enabled=True,
            kline_governance_schedule_time="17:00",
            kline_governance_run_immediately=False,
        )
        scheduler = mock.MagicMock()

        with patch.object(main_module, "setup_logging"), patch.object(
            main_module, "parse_arguments", return_value=args
        ), patch.object(
            main_module, "get_config", return_value=config
        ), patch.object(
            main_module, "run_kline_governance_workflow", return_value={"run_result": "succeeded", "pass_status": "passed"}
        ) as mock_governance_workflow, patch("src.scheduler.Scheduler", return_value=scheduler):
            exit_code = main_module.main()

        self.assertEqual(exit_code, 0)
        scheduler.add_daily_task.assert_any_call(
            name="kline_governance",
            task=mock.ANY,
            schedule_time="17:00",
            run_immediately=False,
        )
        governance_callback = next(
            call.kwargs["task"]
            for call in scheduler.add_daily_task.call_args_list
            if call.kwargs["name"] == "kline_governance"
        )
        governance_callback()
        mock_governance_workflow.assert_called_once_with(config=config, args=args)
        scheduler.run.assert_called_once()

    def test_kline_governance_schedule_registers_deep_audit_as_independent_job(self) -> None:
        args = self._build_args(schedule=True)
        config = self._build_config(
            kline_governance_enabled=True,
            kline_governance_schedule_time="17:00",
            kline_governance_run_immediately=False,
            kline_deep_audit_schedule_enabled=True,
            kline_deep_audit_schedule_time="20:30",
        )
        scheduler = mock.MagicMock()

        with patch.object(main_module, "setup_logging"), patch.object(
            main_module, "parse_arguments", return_value=args
        ), patch.object(
            main_module, "get_config", return_value=config
        ), patch.object(
            main_module, "run_kline_governance_workflow", return_value={"run_result": "succeeded", "pass_status": "passed"}
        ) as mock_governance_workflow, patch.object(
            main_module, "run_kline_deep_audit_workflow", return_value={"run_result": "succeeded", "pass_status": "passed"}
        ) as mock_deep_audit_workflow, patch("src.scheduler.Scheduler", return_value=scheduler):
            exit_code = main_module.main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(scheduler.add_daily_task.call_count, 3)
        scheduler.add_daily_task.assert_any_call(
            name="kline_governance",
            task=mock.ANY,
            schedule_time="17:00",
            run_immediately=False,
        )
        scheduler.add_daily_task.assert_any_call(
            name="kline_deep_audit",
            task=mock.ANY,
            schedule_time="20:30",
            run_immediately=False,
        )
        governance_callback = next(
            call.kwargs["task"]
            for call in scheduler.add_daily_task.call_args_list
            if call.kwargs["name"] == "kline_governance"
        )
        deep_audit_callback = next(
            call.kwargs["task"]
            for call in scheduler.add_daily_task.call_args_list
            if call.kwargs["name"] == "kline_deep_audit"
        )
        governance_callback()
        deep_audit_callback()
        mock_governance_workflow.assert_called_once_with(config=config, args=args)
        mock_deep_audit_workflow.assert_called_once_with(config=config, args=args)
        scheduler.run.assert_called_once()

    def test_main_returns_non_zero_when_immediate_scheduled_screening_fails(self) -> None:
        args = self._build_args(screening=True, schedule=True)
        config = self._build_config()

        with patch.object(main_module, "setup_logging"), patch.object(
            main_module, "parse_arguments", return_value=args
        ), patch.object(
            main_module, "get_config", return_value=config
        ), patch.object(main_module, "run_screening_workflow", side_effect=RuntimeError("同步失败")), patch(
            "src.scheduler.Scheduler"
        ) as mock_scheduler:
            exit_code = main_module.main()

        self.assertEqual(exit_code, 1)
        mock_scheduler.assert_not_called()

    def test_main_returns_non_zero_when_screening_workflow_fails(self) -> None:
        args = self._build_args(screening=True)
        config = self._build_config()

        with patch.object(main_module, "setup_logging"), patch.object(
            main_module, "parse_arguments", return_value=args
        ), patch.object(
            main_module, "get_config", return_value=config
        ), patch.object(main_module, "run_screening_workflow", side_effect=RuntimeError("同步失败")):
            exit_code = main_module.main()

        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
